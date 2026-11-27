"""SD-WAN app rules in two phases: deploy the spokes without the O365-Teams / Vimeo rules (fast), then let the
FortiGates update their FortiGuard application database and add the rules with one more Quick install.
python -m tests.test_app_rules"""
import copy, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
from app import fmg as fm, fmgapply, fmgtemplates as ft, model  # noqa: E402

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))


def topo():
    t = model.merged({"sdwan_app_rules": True})
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.10.106.251/24", gateway="10.10.106.1")
    t["wanemu"]["uplink"].update(ip="10.10.106.250/24", gw="10.10.106.1")
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    return t


# ---- phase 1: the app rules are taken out of the spoke templates and kept for phase 2
plan = ft.build_plan(topo(), ORCH)
moved = ft.defer_app_rules(plan)
edges = [r for r in plan["roles"] if r["kind"] == "edge"]
assert moved == [r["name"] for r in edges] and edges, moved
for r in edges:
    assert [s["id"] for s in r["sdwan"]["service"]] == [1, 10], r["sdwan"]["service"]
    assert [s["id"] for s in r["app_sdwan"]["service"]] == [1, 2, 3, 10]
assert not any("app_sdwan" in r for r in plan["roles"] if r["kind"] == "hub")
p2 = ft.build_plan(dict(topo(), sdwan_app_rules=False), ORCH)
assert ft.defer_app_rules(p2) == []                                  # switch off: nothing to do in phase 2
print("OK  fase 1: spoke-templates zonder O365-Teams/Vimeo, de regels bewaard voor fase 2")

# ---- the FortiGuard application database version from 'diagnose autoupdate versions'
OLD = """Attack Definitions
---------
Version: 6.00741 signed
Last Updated using manual update on Tue Dec  1 02:30:00 2015

Application Definitions
---------
Version: 6.00741 signed
Contract Expiry Date: Tue Sep 28 2027
Last Updated using manual update on Tue Dec  1 02:30:00 2015
Last Update Attempt: n/a
Result: Updates Installed

GenAI Application Definitions
---------
Version: 0.00000
"""
NEW = OLD.replace("Version: 6.00741 signed\nContract", "Version: 38.00349 signed\nContract").replace(
    "on Tue Dec  1 02:30:00 2015\nLast Update Attempt", "on Thu Nov 26 00:10:00 2026\nLast Update Attempt")
assert fm.app_db(OLD) == ("6.00741", 2015) and fm.app_db(NEW) == ("38.00349", 2026), (fm.app_db(OLD), fm.app_db(NEW))
assert fm.app_db("nothing") == ("", 0)
print("OK  versie van de applicatiedatabase uit diagnose autoupdate versions")


# ---- phase 2 with fakes: SSH (versions, update-now), FortiManager (ISDB list, template, Quick install)
class Sh:
    def __init__(self, dev, state): self.dev, self.state = dev, state
    def run(self, line, timeout=30, shown=None):
        st = self.state[self.dev]
        st["cmds"].append(line)
        if line.startswith("execute update-now"):
            st["updating"] = True
            return "site # "
        if line.startswith("diagnose autoupdate versions"):
            if st["updating"] and st["polls"] >= st["after"]:
                return NEW
            if st["updating"]:
                st["polls"] += 1
            return OLD if not st.get("fresh") else NEW
        return ""
    def close(self): pass


class Fmg:
    dry = False
    def __init__(self): self.calls = []
    def call(self, method, url, data=None, **kw):
        self.calls.append((method, url, data))
        if url.endswith("/obj/firewall/internet-service-name"):
            return {"data": [{"name": n} for n in ("Microsoft-Skype_Teams", "Microsoft-Office365", "Microsoft-Outlook")]}
        if url == "/securityconsole/install/device":
            return {"data": {"task": 7}}
        return {"data": None}
    def wait_task(self, tid, timeout=600): return {"state": 4}


t = topo()
plan = ft.build_plan(t, ORCH)
ft.defer_app_rules(plan)
spokes = [d["name"] for d in t["edges"]]
state = {n: {"cmds": [], "updating": False, "polls": 0, "after": 1} for n in spokes}
state[spokes[2]]["fresh"] = True                                     # already up to date: no update-now
state[spokes[1]]["after"] = 10 ** 6                                  # never updates: timeout -> warning, rules anyway
conns = []


def connector(host, user, password, port=22, **kw):
    dev = next(f["name"] for f in __import__("app.wanemu.net", fromlist=["x"]).forwards(t) if f["ssh"] == port)
    conns.append((host, user, password, port))
    return Sh(dev, state)


f, ev = Fmg(), []
boot = {"dry": False, "fgt_user": "admin", "fgt_new_pw": "pw", "fgt_connector": connector,
        "app_timeout": 0.3, "app_poll": 0.01}
fm.app_rules_phase(f, t, plan, boot, lambda s, st, m: ev.append((s, st, m)))
assert ev[0][0] == "app_rules" and ev[0][1] == "running" and "longer" in ev[0][2].lower(), ev[0]     # the warning first
assert all(h == "10.10.106.250" and u == "admin" and p == "pw" for h, u, p, _ in conns), conns       # via wan-emu forwards
assert any(c.startswith("execute update-now") for c in state[spokes[0]]["cmds"])
assert not any(c.startswith("execute update-now") for c in state[spokes[2]]["cmds"])                # already fresh
assert any(s == "app_rules" and st == "warn" and spokes[1] in m for s, st, m in ev), ev               # timed out
upd = [d for m, u, d in f.calls if u.endswith("/system/sdwan") and "wanprof" in u]
assert upd and all([s["id"] for s in d["service"]] == [1, 2, 3, 10] for d in upd), upd
o365 = next(s for s in upd[0]["service"] if s["name"] == "O365-Teams")
assert o365["internet-service-name"] == ["Microsoft-Skype_Teams", "Microsoft-Office365", "Microsoft-Outlook"]
assert "_isdb_names" not in str(f.calls)
inst = [d["scope"][0]["name"] for m, u, d in f.calls if u == "/securityconsole/install/device"]
assert sorted(inst) == sorted(spokes), inst                                                            # one Quick install each
assert ev[-1][0] == "app_rules" and ev[-1][1] in ("ok", "warn")
print("OK  fase 2: waarschuwing, update-now waar nodig, wachten (timeout = melding), regels in het template, Quick install")

# SSH unreachable: a warning per spoke, the rules still go in
f, ev = Fmg(), []
def bad(*a, **k): raise OSError("connection refused")
fm.app_rules_phase(f, t, copy.deepcopy(plan), dict(boot, fgt_connector=bad), lambda s, st, m: ev.append((s, st, m)))
assert sum(1 for s, st, m in ev if st == "warn" and "refused" in m) == len(spokes), ev
assert any(u.endswith("/system/sdwan") for _, u, _ in f.calls)
print("OK  fase 2: geen SSH = waarschuwing per spoke, regels toch toegevoegd")
