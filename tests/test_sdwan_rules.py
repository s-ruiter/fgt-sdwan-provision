"""Demo SD-WAN rules for the Traffic-Gen apps, always on in the SD-WAN template of spokes with two WANs:
O365 + Collab over wan2 (Collab on SLA), streaming/social wan1 only, updates/downloads load-balanced, SaaS on the
lowest latency. Apps are matched with ISDB internet services the FortiManager actually has (keywords resolved at
deployment)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fmgtemplates as ft, model  # noqa: E402

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))


def topo(multi):
    t = model.merged({"region": dict(model.DEFAULTS["region"], multi_vrf=multi)})
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.127.200.1", "100.127.200.1"
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    return t


def two_wan(r):
    return len([m for m in r["sdwan"].get("members", []) if m.get("zone") == "underlay"]) == 2


for multi in (True, False):
    plan = ft.build_plan(topo(multi), ORCH)
    edges2 = [r for r in plan["roles"] if r["kind"] == "edge" and two_wan(r)]
    others = [r for r in plan["roles"] if r not in edges2]
    assert edges2 and any(r["kind"] == "edge" for r in others), [r["name"] for r in plan["roles"]]   # site1-2 has one WAN
    for r in edges2:
        sv = {s["id"]: s for s in r["sdwan"]["service"]}
        assert [s["id"] for s in r["sdwan"]["service"]] == [1, 2, 3, 4, 5, 6, 10], [s["id"] for s in r["sdwan"]["service"]]
        assert [sv[i]["name"] for i in (2, 3, 4, 5, 6)] == ["O365", "Collab", "Streaming-Social", "Updates-Downloads", "SaaS"]
        assert sv[2]["mode"] == "priority" and sv[2]["priority-members"] == ["2", "1"]
        assert sv[3]["mode"] == "sla" and sv[3]["priority-members"] == ["2", "1"] and sv[3]["sla"] == [{"health-check": "Internet", "id": 1}]
        assert sv[4]["priority-members"] == ["1"]
        assert sv[5]["mode"] == "load-balance" and sv[5]["priority-members"] == ["1", "2"]
        assert sv[6]["mode"] == "priority" and sv[6]["link-cost-factor"] == "latency" and sv[6]["health-check"] == "Internet"
        for i in (2, 3, 4, 5, 6):
            assert sv[i]["_isdb_keywords"] and "dst" not in sv[i], sv[i]
        hc = {h["name"]: h for h in r["sdwan"]["health-check"]}["Internet"]
        assert hc["sla"] == [{"id": 1, "latency-threshold": 150, "jitter-threshold": 30, "packetloss-threshold": 2}], hc
    for r in others:
        assert not any(2 <= s.get("id", 0) <= 6 for s in r["sdwan"].get("service", [])), r["name"]
print("OK  demo-regels: spokes met 2 WAN's krijgen O365/Collab/Streaming/Updates/SaaS (ids 2-6) tussen Corporate en Local-Breakout")
