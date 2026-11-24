"""Single-VRF vs multi-VRF template generation (no servers needed).  python -m tests.test_singlevrf"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fmgtemplates as ft, model
ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))


def topo(multi):
    t = model.merged({"region": dict(model.DEFAULTS["region"], multi_vrf=multi)})
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.127.200.1", "100.127.200.1"
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
# single: none of that, one LAN, no vrf-select, no mgmt template at all (hub gateway is on the SD-WAN member)
assert not re.search(r"set vrf (1|11|12)\b|vrf11_leak|vpn-id-ipip|vrf-select", ts), re.findall(r"set vrf \d+|vrf11_leak|vpn-id-ipip|vrf-select", ts)
assert "lan2_ip" not in "".join(ps["variables"]) and "lan_ip" in ps["variables"]
assert "encapsulation none" in ts
assert not any(n.endswith("00-Mgmt") for r in ps["roles"] if r["kind"] == "hub" for n, _, _ in r["cli"])  # no static route
assert not any(n.endswith("00-Mgmt") for r in ps["roles"] if r["kind"] == "edge" for n, _, _ in r["cli"])  # dhcp: nothing to add
assert not any(p["name"] == "Internet (to PE)" for r in ps["roles"] for p in r["policies"])
assert any(p["name"] == "Internet (to PE)" for r in pm["roles"] for p in r["policies"])
# validation
bad = topo(False); bad["region"]["lan_summary"] = "nope"
assert any("LAN summary" in e for e in model.validate(bad))
bad = topo(True); bad["region"]["vrfs"] = []
assert any("at least 1 vrf" in e.lower() for e in model.validate(bad))
print("OK  single-VRF: geen VRF 1/11/12, leak, vpn-id-ipip of vrf-select; 1 LAN; multi-VRF ongewijzigd; validatie")

# single-VRF: everything in VRF 0 (only explicit 'set vrf 0'), FMG tunnel without vrf-select
import json as _j  # noqa: E402
for r in ps["roles"]:
    blob = "\n".join(x for _, x, _ in r["cli"]) + _j.dumps(r["sdwan"]) + _j.dumps(r["static"]) + _j.dumps(r["policies"])
    vrfs = set(re.findall(r"set vrf (\S+)", blob)) | set(re.findall(r'"vrf": ?"?(\w+)', blob))
    assert vrfs <= {"0"} and "vrf-select" not in blob, (r["name"], vrfs)
print("OK  single-VRF: alles in VRF 0, geen vrf-select")

# a new deployment defaults to single-VRF (everything in VRF 0); multi-VRF is an explicit choice
from app import model as _m  # noqa: E402
assert _m.DEFAULTS["region"]["multi_vrf"] is False and not _m._multi(_m.merged({}))
print("OK  standaard: single-VRF")

# multi-VRF: DIA on for every VRF by default; the templates follow the choice per VRF (leak to the PE-VRF + default
# route only for a VRF with DIA); the WAN members themselves are always DIA
assert all(v["dia"] is True for v in _m.DEFAULTS["region"]["vrfs"])
on = topo(True)
off = topo(True); off["region"]["vrfs"][1]["dia"] = False
t_on, t_off = text(ft.build_plan(on, ORCH)), text(ft.build_plan(off, ORCH))
assert "vrf11_leak" in t_on and "vrf12_leak" in t_on and "edit 4012" in t_on
assert "vrf11_leak" in t_off and "vrf12_leak" not in t_off and "edit 4012" not in t_off
assert "'dia': true" in _m.build(on)[0].split("wan2_intf")[1][:80]                     # spoke wan2: DIA too
# LAN2+ per VRF is optional: no port = no LAN in that VRF (also not sent to the templates)
o = topo(True); o["edges"][0]["lans"][1]["intf"] = ""
assert len(_m.merged(o)["edges"][0]["lans"]) == 1 and model.validate(_m.merged(o)) == []
print("OK  multi-VRF: DIA standaard aan per VRF, templates volgen de keuze; LAN2 zonder poort = geen LAN")

# multi-VRF hub: the LAN is in VRF 11/12 and its default route goes over the leak link to the PE-VRF, so DIA needs
# lan_zone -> vrfs_leak_zone and then pevrf_leak_zone -> underlay (the Orchestrator hub firewall only has
# lan_zone -> underlay, which never matches there). Single-VRF hubs keep lan_zone -> underlay.
def pol(plan, role):
    return [(p["name"], p["srcintf"], p["dstintf"], p.get("nat")) for r in plan["roles"] if r["name"] == role
            for p in r["policies"]]


for role in ("HUB1", "HUB2"):
    hp = pol(pm, role)
    names = [p[0] for p in hp]
    assert ("Internet (to PE)", ["lan_zone"], ["vrfs_leak_zone"], "disable") in hp, hp
    assert ("Internet (DIA)", ["pevrf_leak_zone"], ["underlay"], "enable") in hp, hp
    assert names.index("Internet (to PE)") < names.index("Internet (DIA)")
    assert not any(p[1] == ["lan_zone"] and p[2] == ["underlay"] for p in hp)
    r = [x for x in pm["roles"] if x["name"] == role][0]
    assert {"pevrf_leak_zone", "vrfs_leak_zone"} <= set(r["dyn_ifaces"])
    assert len({p["policyid"] for p in r["policies"]}) == len(r["policies"])
    assert ("Internet (DIA)", ["lan_zone"], ["underlay"], "enable") in pol(ps, role)
    assert "Internet (to PE)" not in [p[0] for p in pol(ps, role)]
sp = pol(pm, "SPOKES") or pol(pm, "SPOKES-1")
assert ("Internet (DIA)", ["pevrf_leak_zone"], ["underlay"], "enable") in sp and ("Internet (to PE)", ["lan_zone"], ["vrfs_leak_zone"], "disable") in sp
print("OK  multi-VRF hub: DIA via leak (lan_zone -> vrfs_leak_zone, pevrf_leak_zone -> underlay); single-VRF ongewijzigd")

# multi-VRF DIA/RIA from a LAN: no NAT from the VRF to the PE-VRF. FortiOS treats an address the FortiGate NATs to
# (leak IP or ip-pool) as local: the reply in the PE-VRF is delivered to the FortiGate itself instead of over the leak
# link (flow trace 'find a route: flag=80000000 gw-0.0.0.0 via root', measured on site1-1, FortiOS VM). Instead the
# PE-VRF gets a return route per LAN with DIA: LAN subnet -> vrfN_leak0, gateway = the vrfN_leak1 address.
for r in pm["roles"]:
    to_pe = [p for p in r["policies"] if p["name"] == "Internet (to PE)"]
    assert to_pe and all(p.get("nat") == "disable" and "ippool" not in p for p in to_pe), (r["name"], to_pe)
    cli = "\n".join(x for _, x, _ in r["cli"])
    assert re.search(r'edit 4111\s+set dst \$\(lan_ip_subnet\)\s+set gateway 10\.200\.255\.22\s+set device "vrf11_leak0"', cli), (r["name"], cli[-600:])
    if r["kind"] == "edge":
        assert re.search(r'edit 4112\s+set dst \$\(lan2_ip_subnet\)\s+set gateway 10\.200\.255\.24\s+set device "vrf12_leak0"', cli)
e1 = m["edges"][0]["name"]
assert pm["variables"]["lan_ip_subnet"][e1] == "10.0.1.0 255.255.255.0" and pm["variables"]["lan2_ip_subnet"][e1] == "10.0.101.0 255.255.255.0"
assert "4111" not in text(ps) and "_subnet" not in "".join(ps["variables"])
# a VRF without DIA: no leak, no return route
assert "edit 4112" not in text(ft.build_plan(off, ORCH))
# the return routes need distinct LAN subnets per FortiGate across its VRFs
ov = topo(True); ov["edges"][0]["lans"][1]["ip"] = "10.0.1.129/25"
assert any("overlap" in e for e in model.validate(ov)), model.validate(ov)
print("OK  multi-VRF: naar PE zonder NAT + return route per LAN in de PE-VRF; overlappende LAN's per FortiGate geweigerd")
