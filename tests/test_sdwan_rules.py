"""SD-WAN rules of spokes with two WANs: Corporate (overlay), O365-Teams on SLA 'Internet' (wan1, then wan2) with
three fixed internet services, Local-Breakout wan2 first. The internet-service names are checked against the
FortiManager's list at deployment (a name it does not have is left out, with a warning)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fmgtemplates as ft, model  # noqa: E402

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))


def topo(multi, one_wan=True):
    t = model.merged({"region": dict(model.DEFAULTS["region"], multi_vrf=multi)})
    if one_wan:                                  # site1-2 with one WAN: its own role without the wan2 rules
        t["edges"][1]["wan2_intf"] = ""
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.127.200.1", "100.127.200.1"
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    return t


def two_wan(r):
    return len([m for m in r["sdwan"].get("members", []) if m.get("zone") == "underlay"]) == 2


# defaults: every spoke has two WANs -> one SPOKES role (no SPOKES-2)
assert all(e["wan2_intf"] for e in model.DEFAULTS["edges"]), [e["name"] for e in model.DEFAULTS["edges"] if not e["wan2_intf"]]
for multi in (True, False):
    plan = ft.build_plan(topo(multi, one_wan=False), ORCH)
    edge_roles = [r for r in plan["roles"] if r["kind"] == "edge"]
    assert [r["name"] for r in edge_roles] == ["SPOKES"] and two_wan(edge_roles[0]), [r["name"] for r in edge_roles]
    assert edge_roles[0]["devices"] == ["site1-1", "site1-2", "site1-3"], edge_roles[0]["devices"]
print("OK  defaults: alle spokes twee WANs, een SPOKES-template")

O365 = ["Microsoft-Skype_Teams", "Microsoft-Office365", "Microsoft-Outlook"]
for multi in (True, False):
    plan = ft.build_plan(topo(multi), ORCH)
    edges2 = [r for r in plan["roles"] if r["kind"] == "edge" and two_wan(r)]
    others = [r for r in plan["roles"] if r not in edges2]
    assert edges2 and any(r["kind"] == "edge" for r in others), [r["name"] for r in plan["roles"]]   # site1-2 has one WAN
    for r in edges2:
        under = [str(m["seq-num"]) for m in r["sdwan"]["members"] if m.get("zone") == "underlay"]
        sv = {s["id"]: s for s in r["sdwan"]["service"]}
        assert [s["id"] for s in r["sdwan"]["service"]] == [1, 2, 10], [s["id"] for s in r["sdwan"]["service"]]
        assert sv[2]["name"] == "O365-Teams" and sv[2]["mode"] == "sla", sv[2]
        assert sv[2]["priority-members"] == under and sv[2]["sla"] == [{"health-check": "Internet", "id": 1}], sv[2]
        assert sv[2]["_isdb_names"] == O365 and "dst" not in sv[2], sv[2]
        assert sv[10]["name"] == "Local-Breakout" and sv[10]["priority-members"] == under[::-1], sv[10]   # wan2 first
        hc = {h["name"]: h for h in r["sdwan"]["health-check"]}["Internet"]
        assert hc["sla"] == [{"id": 1, "latency-threshold": 150, "jitter-threshold": 30, "packetloss-threshold": 2}], hc
    for r in others:
        assert not any(2 <= s.get("id", 0) <= 6 for s in r["sdwan"].get("service", [])), r["name"]
        if r["kind"] == "edge":                                   # one WAN: Local-Breakout as rendered
            lb = [s for s in r["sdwan"]["service"] if s.get("name") == "Local-Breakout"]
            assert all(len(s["priority-members"]) == 1 for s in lb), lb
# member numbers come from the template's underlay members (wan1 first, wan2 second), not fixed values
sd = {"members": [{"interface": "port1", "zone": "underlay", "seq-num": 5}, {"interface": "port2", "zone": "underlay", "seq-num": 6},
                  {"interface": "H1-WAN1", "zone": "overlay", "seq-num": 1}],
      "health-check": [{"name": "Internet", "members": ["5", "6"]}],
      "service": [{"id": 1, "name": "Corporate"}, {"id": 10, "name": "Local-Breakout", "priority-members": ["5", "6"]}]}
ft.add_demo_rules(sd)
sv = {s["name"]: s for s in sd["service"]}
assert [s["name"] for s in sd["service"]] == ["Corporate", "O365-Teams", "Local-Breakout"]
assert sv["O365-Teams"]["priority-members"] == ["5", "6"] and sv["Local-Breakout"]["priority-members"] == ["6", "5"]
print("OK  spokes met 2 WAN's: Corporate, O365-Teams (SLA, wan1 dan wan2), Local-Breakout wan2 eerst")

# ---- the fixed internet-service names are checked against the FortiManager's list at deployment
import copy  # noqa: E402

from app import fmgapply  # noqa: E402
from app.fmg import FmgError  # noqa: E402

NAMES = ["Microsoft-Office365", "Microsoft-Office365.Published", "Microsoft-Skype_Teams", "Microsoft-Outlook", "Zoom.us-Zoom.Meeting"]
base = ft.build_plan(topo(True), ORCH)


def o365(p):
    r = [x for x in p["roles"] if x["kind"] == "edge" and two_wan(x)][0]
    return {s["name"]: s for s in r["sdwan"]["service"]}.get("O365-Teams")


p = copy.deepcopy(base)
assert fmgapply.resolve_isdb(NAMES, p) == []
assert o365(p)["internet-service"] == "enable" and o365(p)["internet-service-name"] == O365, o365(p)   # exact, in order
assert "_isdb_names" not in str(p["roles"])
p = copy.deepcopy(base)                                             # a name the FortiManager lacks: left out, warning
w = fmgapply.resolve_isdb([n for n in NAMES if n != "Microsoft-Outlook"], p)
assert o365(p)["internet-service-name"] == ["Microsoft-Skype_Teams", "Microsoft-Office365"]
assert len(w) == 1 and "Microsoft-Outlook" in w[0] and "O365-Teams" in w[0], w
p = copy.deepcopy(base)                                             # none of them: rule left out
w = fmgapply.resolve_isdb(["Netflix-Netflix"], p)
assert o365(p) is None and any("O365-Teams" in x and "skipped" in x for x in w), w
p = copy.deepcopy(base)                                             # no list from the FortiManager
w = fmgapply.resolve_isdb([], p)
assert len(w) == 1 and "skipped" in w[0] and o365(p) is None
r = [x for x in p["roles"] if x["kind"] == "edge" and two_wan(x)][0]
assert [s["id"] for s in r["sdwan"]["service"]] == [1, 10]


class Fmg:
    dry = False

    def __init__(self, names=None, fail=False):
        self.calls, self.names, self.fail = [], names or [], fail

    def call(self, method, url, data=None, **kw):
        self.calls.append((method, url, data))
        if url.endswith("/obj/firewall/internet-service-name"):
            if self.fail:
                raise FmgError("no permission")
            if "get reserved" not in (kw.get("option") or []):      # like FMG 8.0.2: built-in ISDB only with this option
                return {"data": []}
            return {"data": [{"name": n} for n in self.names]}
        return {"data": None}


t = topo(True)
ev = []
f = Fmg(fail=True)
fmgapply.apply_plan(f, t, copy.deepcopy(base), lambda st, s, m: ev.append((s, m)))
assert f.calls[0][1] == f"/pm/config/adom/{t['adom']}/obj/firewall/internet-service-name"
demo = [m for s, m in ev if s == "warn"]
assert len(demo) == 1 and "no permission" in demo[0], demo
f = Fmg(NAMES)
fmgapply.apply_plan(f, t, copy.deepcopy(base), lambda *a: None)
assert any("Microsoft-Skype_Teams" in str(d) for _, _, d in f.calls) and "_isdb_names" not in str(f.calls)
f = Fmg(NAMES); f.dry = True                                                        # dry run: no lookup
fmgapply.apply_plan(f, t, copy.deepcopy(base), lambda *a: None)
assert not any(u.endswith("internet-service-name") for _, u, _ in f.calls) and "_isdb_names" not in str(f.calls)
print("OK  ISDB: vaste namen gecontroleerd tegen de FortiManager; ontbrekende naam weggelaten met melding")
