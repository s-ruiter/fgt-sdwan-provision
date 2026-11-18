"""WAN-emulator: model defaults, validation and the pure network functions (MACs, reservations, links, dnsmasq, LXC)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model  # noqa: E402
from app.wanemu import net  # noqa: E402

t = model.merged({})
m = net.mac_for("site1-1", 1)
assert m == net.mac_for("site1-1", 1) and m != net.mac_for("site1-1", 2) and m.startswith("02:")
assert re.fullmatch(r"02(:[0-9a-f]{2}){5}", m)
assert [(h["wan1_ip"], h["wan1_gw"]) for h in t["hubs"]] == [("100.127.200.11/24", "100.127.200.1"),
                                                            ("100.127.200.12/24", "100.127.200.1")]
r = net.reservations(t)
assert [(x["name"], x["seg"], x["ip"]) for x in r[:2]] == [("site1-1", "a", "100.127.201.11"), ("site1-1", "b", "100.127.202.11")]
assert r[0]["mac"] == net.mac_for("site1-1", 1) and r[1]["mac"] == net.mac_for("site1-1", 2)
assert ("site1-3", "a", "100.127.201.13") in [(x["name"], x["seg"], x["ip"]) for x in r]
L = net.links(t)
assert {"id": "site1-H1-wan1", "name": "site1-H1", "seg": "hub", "iface": "eth1", "ip": "100.127.200.11"} in L
assert len(L) == 7   # 2 hubs + site1-1 (2) + site1-2 (alleen wan1) + site1-3 (2)
assert not any(x["name"] == "site1-2" and x["seg"] == "b" for x in r + L)          # spoke zonder wan2
c = net.dnsmasq_conf(t)
assert "interface=eth2" in c and "interface=eth3" in c and "interface=eth1" not in c
assert "dhcp-range=set:a,100.127.201.100,100.127.201.199,255.255.255.0,12h" in c
assert f"dhcp-host={net.mac_for('site1-1', 1)},100.127.201.11" in c and "dhcp-option=tag:a,option:router,100.127.201.1" in c
assert "dhcp-option=tag:a,option:dns-server,1.1.1.1,8.8.8.8" in c
p = net.ct_params(t, 130, "ssh-ed25519 AAAA test")
assert p["net0"] == "name=eth0,bridge=vmbr0,tag=106,ip=10.10.106.250/24,gw=10.10.106.1", p["net0"]
assert p["net2"] == "name=eth2,bridge=vmbr0,tag=3201,ip=100.127.201.1/24", p["net2"]
assert p["unprivileged"] == 1 and p["hostname"] == "wan-emu" and "fgt-sdwan" in p["tags"] and p["vmid"] == 130
assert p["ostemplate"].startswith("local:vztmpl/debian-13") and p["ssh-public-keys"] == "ssh-ed25519 AAAA test"
at = net.agent_topology(t)
assert at["links"] == L and at["nat"] == ["100.127.200.0/24", "100.127.201.0/24", "100.127.202.0/24"]
print("OK  netwerkfuncties: MAC stabiel, reserveringen .11+n, 7 links, dnsmasq per segment, LXC-params, agent-topologie")

# validation
assert model.validate_wanemu(t) == [], model.validate_wanemu(t)
bad = model.merged({}); bad["hubs"][0]["wan1_ip"] = "10.10.106.101/24"
assert any("ISP-HUB" in e for e in model.validate_wanemu(bad))
bad = model.merged({}); bad["hubs"][0]["wan1_gw"] = "100.127.200.5"
assert any("gateway" in e and "ISP-HUB" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"segments": {"b": {"vlan": 3201}}}})
assert bad["wanemu"]["segments"]["b"]["subnet"] == "100.127.202.0/24"               # deep merge, rest blijft
assert any("VLAN" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"segments": {"b": {"subnet": "100.127.201.0/25", "gw": "100.127.201.1"}}}})
assert any("overlap" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"uplink": {"gw": "10.10.107.1"}}})
assert any("uplink" in e.lower() for e in model.validate_wanemu(bad))
bad = model.merged({}); bad["edges"] = bad["edges"] * 30
assert any("spokes" in e for e in model.validate_wanemu(bad))
pv = dict(model.merged({})); pv["pve"].update(url="https://pve:8006", token_id="a@pam!b", node="n", disk_storage="d",
                                              import_storage="i", iso_storage="s")
pv["hubs"][0]["wan1_ip"] = "10.1.1.1/24"; pv["hubs"][0]["wan1_gw"] = "10.1.1.2"
assert any("ISP-HUB" in e for e in model.validate_pve(pv))                          # Day-0 checkt de WAN-emulator
print("OK  validatie: hub in ISP-HUB, gateway, VLAN's uniek, geen overlap, uplink, aantal spokes")
