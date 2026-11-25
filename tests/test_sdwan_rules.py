"""Demo SD-WAN rules for the Traffic-Gen apps, always on in the SD-WAN template of spokes with two WANs:
O365 + Collab over wan2 (Collab on SLA), streaming/social wan1 only, updates/downloads load-balanced, SaaS on the
lowest latency. Apps are matched with ISDB internet services the FortiManager actually has (keywords resolved at
deployment)."""
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

for multi in (True, False):
    plan = ft.build_plan(topo(multi), ORCH)
    edges2 = [r for r in plan["roles"] if r["kind"] == "edge" and two_wan(r)]
    others = [r for r in plan["roles"] if r not in edges2]
    assert edges2 and any(r["kind"] == "edge" for r in others), [r["name"] for r in plan["roles"]]   # site1-2 has one WAN
    for r in edges2:
        sv = {s["id"]: s for s in r["sdwan"]["service"]}
        assert [s["id"] for s in r["sdwan"]["service"]] == [1, 2, 3, 4, 5, 6, 10], [s["id"] for s in r["sdwan"]["service"]]
        assert [sv[i]["name"] for i in (2, 3, 4, 5, 6)] == ["O365", "Collab", "Streaming-Social", "Updates-Downloads", "SaaS"]
        assert sv[2]["mode"] == "sla" and sv[2]["priority-members"] == ["2", "1"] and sv[2]["sla"] == [{"health-check": "Internet", "id": 1}]
        assert sv[3]["mode"] == "sla" and sv[3]["priority-members"] == ["2", "1"] and sv[3]["sla"] == [{"health-check": "Internet", "id": 1}]
        assert sv[4]["priority-members"] == ["1"]
        assert sv[5]["mode"] == "load-balance" and sv[5]["priority-members"] == ["1", "2"] and sv[5]["sla"] == [{"health-check": "Internet", "id": 1}]
        assert sv[6]["mode"] == "priority" and sv[6]["link-cost-factor"] == "latency" and sv[6]["health-check"] == "Internet"
        for i in (2, 3, 4, 5, 6):
            assert sv[i]["_isdb_keywords"] and "dst" not in sv[i], sv[i]
        hc = {h["name"]: h for h in r["sdwan"]["health-check"]}["Internet"]
        assert hc["sla"] == [{"id": 1, "latency-threshold": 150, "jitter-threshold": 30, "packetloss-threshold": 2}], hc
    for r in others:
        assert not any(2 <= s.get("id", 0) <= 6 for s in r["sdwan"].get("service", [])), r["name"]
# member numbers come from the template's underlay members (wan1 first, wan2 second), not fixed values
sd = {"members": [{"interface": "port1", "zone": "underlay", "seq-num": 5}, {"interface": "port2", "zone": "underlay", "seq-num": 6},
                  {"interface": "H1-WAN1", "zone": "overlay", "seq-num": 1}],
      "health-check": [{"name": "Internet", "members": ["5", "6"]}], "service": [{"id": 1, "name": "Corporate"}, {"id": 10, "name": "x"}]}
ft.add_demo_rules(sd)
sv = {s["name"]: s for s in sd["service"]}
assert sv["O365"]["priority-members"] == ["6", "5"] and sv["Streaming-Social"]["priority-members"] == ["5"]
assert sv["Updates-Downloads"]["priority-members"] == ["5", "6"]
print("OK  demo-regels: spokes met 2 WAN's krijgen O365/Collab/Streaming/Updates/SaaS (ids 2-6) tussen Corporate en Local-Breakout")

# ---- resolve the keywords against the FortiManager's internet-service names at deployment
import copy  # noqa: E402

from app import fmgapply  # noqa: E402
from app.fmg import FmgError  # noqa: E402

NAMES = ["Microsoft-Office365", "Microsoft-Office365.Published", "Microsoft-Teams", "Zoom.us-Zoom.Meeting",
         "Netflix-Netflix", "Google-YouTube", "Microsoft-Xbox", "Microsoft-Windows.Update", "Ookla-Speedtest"]
base = ft.build_plan(topo(True), ORCH)
p = copy.deepcopy(base)
w = fmgapply.resolve_isdb(NAMES, p)
r = [x for x in p["roles"] if x["kind"] == "edge" and two_wan(x)][0]
sv = {s["name"]: s for s in r["sdwan"]["service"]}
assert sv["O365"]["internet-service"] == "enable" and sv["O365"]["internet-service-name"] == ["Microsoft-Office365", "Microsoft-Office365.Published"]
assert sv["Collab"]["internet-service-name"] == ["Microsoft-Teams", "Zoom.us-Zoom.Meeting"], sv["Collab"]
assert sv["Streaming-Social"]["internet-service-name"] == ["Google-YouTube", "Netflix-Netflix"]
assert sv["Updates-Downloads"]["internet-service-name"] == ["Microsoft-Windows.Update", "Ookla-Speedtest"]
assert "SaaS" not in sv and "Microsoft-Xbox" not in str(sv)                    # 'box' is not 'Xbox'
assert any("SaaS: no internet services found" in x for x in w), w
assert "_isdb_keywords" not in str(p["roles"])
# no list from the FortiManager: all demo rules skipped, one warning, the rest unchanged
p = copy.deepcopy(base)
w = fmgapply.resolve_isdb([], p)
assert len(w) == 1 and "demo SD-WAN rules skipped" in w[0]
r = [x for x in p["roles"] if x["kind"] == "edge" and two_wan(x)][0]
assert [s["id"] for s in r["sdwan"]["service"]] == [1, 10]
# many hits per keyword: at most 10
many = [f"Vendor-Netflix.{i}" for i in range(30)]
p = copy.deepcopy(base)
fmgapply.resolve_isdb(many, p)
r = [x for x in p["roles"] if x["kind"] == "edge" and two_wan(x)][0]
assert len({s["name"]: s for s in r["sdwan"]["service"]}["Streaming-Social"]["internet-service-name"]) == 10


# apply_plan: reads the names from the ADOM first; the calls to FMG carry no keywords; a failing lookup = a warning
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
for f in (Fmg(NAMES), Fmg(fail=True)):
    ev.clear()
    fmgapply.apply_plan(f, t, copy.deepcopy(base), lambda st, s, m: ev.append((s, m)))
    assert f.calls[0][1] == f"/pm/config/adom/{t['adom']}/obj/firewall/internet-service-name"
    assert "_isdb_keywords" not in str(f.calls)
    assert any(s == "warn" and ("SaaS" in m or "skipped" in m) for s, m in ev), ev
    if f.fail:                                                       # one warning, with the reason
        demo = [m for s, m in ev if s == "warn" and ("internet-service" in m or "demo" in m)]
        assert len(demo) == 1 and "no permission" in demo[0], demo
assert any("Microsoft-Teams" in str(d) for _, _, d in Fmg(NAMES).calls) is False   # (fresh object: nothing called)
f = Fmg(NAMES)
fmgapply.apply_plan(f, t, copy.deepcopy(base), lambda *a: None)
assert any("Microsoft-Teams" in str(d) for _, _, d in f.calls)
f = Fmg(NAMES); f.dry = True                                                        # dry run: no lookup
fmgapply.apply_plan(f, t, copy.deepcopy(base), lambda *a: None)
assert not any(u.endswith("internet-service-name") for _, u, _ in f.calls) and "_isdb_keywords" not in str(f.calls)
print("OK  ISDB: zoekwoorden -> bestaande namen (max 10, woordgrens), regel zonder treffer overgeslagen, geen lijst = melding")
