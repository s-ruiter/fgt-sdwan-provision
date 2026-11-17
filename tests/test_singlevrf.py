"""Single-VRF vs multi-VRF template generation (no servers needed).  python -m tests.test_singlevrf"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fmgtemplates as ft, model
ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))


def topo(multi):
    t = model.merged({})
    t["region"]["multi_vrf"] = multi
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.64.1.6", "100.64.2.6"
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    return t


def text(plan):
    return "\n".join(x for r in plan["roles"] for _, x, _ in r["cli"])


m, s = topo(True), topo(False)
assert model.validate(m) == [] and model.validate(s) == []
pm, ps = ft.build_plan(m, ORCH), ft.build_plan(s, ORCH)
tm, ts = text(pm), text(ps)
# multi: VRF 11/12, leak, vpn-id-ipip, wan1 in PE-VRF, FMG tunnel follows
assert "set vrf 11" in tm and "set vrf 12" in tm and "vrf11_leak" in tm and "vpn-id-ipip" in tm
assert "set vrf-select 1" in tm and "lan2_ip" in "".join(pm["variables"])
# single: none of that, one LAN, no vrf-select, hub mgmt only for static wan1
assert not re.search(r"set vrf (1|11|12)\b|vrf11_leak|vpn-id-ipip|vrf-select", ts), re.findall(r"set vrf \d+|vrf11_leak|vpn-id-ipip|vrf-select", ts)
assert "lan2_ip" not in "".join(ps["variables"]) and "lan_ip" in ps["variables"]
assert "encapsulation none" in ts
assert any(n.endswith("00-Mgmt") for r in ps["roles"] if r["kind"] == "hub" for n, _, _ in r["cli"])      # static wan1 route
assert not any(n.endswith("00-Mgmt") for r in ps["roles"] if r["kind"] == "edge" for n, _, _ in r["cli"])  # dhcp: nothing to add
assert not any(p["name"] == "Internet (to PE)" for r in ps["roles"] for p in r["policies"])
assert any(p["name"] == "Internet (to PE)" for r in pm["roles"] for p in r["policies"])
# validation
bad = topo(False); bad["region"]["lan_summary"] = "nope"
assert any("LAN-summary" in e for e in model.validate(bad))
bad = topo(True); bad["region"]["vrfs"] = []
assert any("minstens 1 vrf" in e.lower() for e in model.validate(bad))
print("OK  single-VRF: geen VRF 1/11/12, leak, vpn-id-ipip of vrf-select; 1 LAN; multi-VRF ongewijzigd; validatie")
