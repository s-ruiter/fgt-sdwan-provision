"""One VLAN + /24 per FortiGate WAN: hub n wan1 100.64.n.0/24 (VLAN 3200+n), spoke n wan1 100.65.n.0/24 (3210+n),
spoke n wan2 100.66.n.0/24 (3230+n); FortiGate .2, wan-emu .1. All traffic of a WAN (shortcuts too) then passes the
wan-emu."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model, settings_io  # noqa: E402
from tests import labuplink  # noqa: E402,F401


def lab(extra=None):
    t = model.merged(extra or {})
    t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
    return t


t = lab()
L = {x["id"]: x for x in model.wan_links(t)}
assert list(L) == ["site1-H1-wan1", "site1-H2-wan1", "site1-1-wan1", "site1-1-wan2", "site1-2-wan1", "site1-3-wan1",
                   "site1-3-wan2"], list(L)
assert L["site1-H1-wan1"] == {"id": "site1-H1-wan1", "name": "site1-H1", "seg": "hub", "n": 1, "subnet": "100.64.1.0/24",
                              "gw": "100.64.1.1", "ip": "100.64.1.2", "vlan": 3201}
assert L["site1-H2-wan1"]["vlan"] == 3202 and L["site1-1-wan1"]["ip"] == "100.65.1.2" and L["site1-1-wan1"]["vlan"] == 3211
assert L["site1-1-wan2"]["ip"] == "100.66.1.2" and L["site1-1-wan2"]["vlan"] == 3231
assert L["site1-3-wan2"]["ip"] == "100.66.3.2" and L["site1-3-wan2"]["vlan"] == 3233 and "site1-2-wan2" not in L
assert t["hubs"][0]["wan1_ip"] == "100.64.1.2/24" and t["hubs"][0]["wan1_gw"] == "100.64.1.1"
assert t["hubs"][1]["wan1_ip"] == "100.64.2.2/24" and model.hub_wan1_ip(t["hubs"][1]) == "100.64.2.2"
assert t["wanemu"]["segments"]["a"] == {"name": "ISP-A", "block": "100.65.0.0/16", "vlan_base": 3210}
assert [e for e in model.validate_wanemu(t)] == [], model.validate_wanemu(t)
print("OK  links: hub/spoke wan1/wan2 eigen /24 en VLAN, hub-adres afgeleid, standaard geldig")


def errs(seg_over=None, **w):
    x = lab({"wanemu": dict(w, **({"segments": seg_over} if seg_over else {}))})
    return " | ".join(model.validate_wanemu(x))


assert "ISP-A: block" in errs({"a": {"block": "nope"}})
assert "ISP-A: block" in errs({"a": {"block": "100.65.0.0/24"}})              # needs room for /24s per spoke
assert "ISP-A: block" in errs({"a": {"block": "100.65.0.0/23"}})               # .0 and .1 = only spoke 1
assert "overlap" in errs({"a": {"block": "100.64.0.0/16"}})                    # same as ISP-HUB
assert "overlap" in errs({"a": {"block": "10.10.0.0/16"}})                     # covers the uplink 10.10.106.0/24
assert "VLAN" in errs({"a": {"vlan_base": 3200}})                              # 3201.. clashes with the hubs
assert "VLAN" in errs({"a": {"vlan_base": 3299}})                              # 3300.. = LAN VLANs
many = lab({"edges": [dict(model.DEFAULTS["edges"][0], name=f"s{i}", loopback=f"10.200.1.{i + 1}") for i in range(15)]})
assert any("31" in e for e in model.validate_wanemu(many)), model.validate_wanemu(many)      # 2 + 30 = 32 links
print("OK  validatie: blok, overlap, VLAN-botsing, maximaal 31 WAN-lijnen")

old = {"name": "ISP-A", "vlan": 3201, "subnet": "100.127.201.0/24", "gw": "100.127.201.1", "res_start": 11, "pool": [100, 199]}
m = lab({"wanemu": {"segments": {"a": old}}})
assert m["wanemu"]["segments"]["a"] == {"name": "ISP-A", "block": "100.65.0.0/16", "vlan_base": 3210}, m["wanemu"]["segments"]["a"]
f = {"format": "fgt-sdwan-settings", "version": 1, "topology": {"wanemu": {"segments": {"a": old}}}}
topo, ui, notes = settings_io.from_file(f, {})
assert topo["wanemu"]["segments"]["a"]["block"] == "100.65.0.0/16" and any("one subnet per WAN" in n for n in notes), notes
print("OK  oude instellingen (subnet/gw per segment): nieuwe standaard + melding")
