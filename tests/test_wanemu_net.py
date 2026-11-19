"""WAN-emulator: model defaults, validation and the pure network functions (MACs, reservations, links, dnsmasq, LXC)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model  # noqa: E402
from app.wanemu import net  # noqa: E402

LAB = {"vlan": 106, "ip": "10.10.106.250/24", "gw": "10.10.106.1"}       # the user's lab uplink (no default)
t = model.merged({"wanemu": {"uplink": LAB}})
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
bad = model.merged({"wanemu": {"uplink": LAB}}); bad["hubs"][0]["wan1_ip"] = "10.10.106.101/24"
assert any("ISP-HUB" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"uplink": LAB}}); bad["hubs"][0]["wan1_gw"] = "100.127.200.5"
assert any("gateway" in e and "ISP-HUB" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"segments": {"b": {"vlan": 3201}}}})
assert bad["wanemu"]["segments"]["b"]["subnet"] == "100.127.202.0/24"               # deep merge, rest blijft
assert any("VLAN" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"segments": {"b": {"subnet": "100.127.201.0/25", "gw": "100.127.201.1"}}}})
assert any("overlap" in e for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"uplink": {"gw": "10.10.107.1"}}})
assert any("uplink" in e.lower() for e in model.validate_wanemu(bad))
bad = model.merged({"wanemu": {"uplink": LAB}}); bad["edges"] = bad["edges"] * 30
assert any("spokes" in e for e in model.validate_wanemu(bad))
pv = dict(model.merged({"wanemu": {"uplink": LAB}})); pv["pve"].update(url="https://pve:8006", token_id="a@pam!b", node="n", disk_storage="d",
                                              import_storage="i", iso_storage="s")
pv["hubs"][0]["wan1_ip"] = "10.1.1.1/24"; pv["hubs"][0]["wan1_gw"] = "10.1.1.2"
assert any("ISP-HUB" in e for e in model.validate_pve(pv))                          # Day-0 checkt de WAN-emulator
print("OK  validatie: hub in ISP-HUB, gateway, VLAN's uniek, geen overlap, uplink, aantal spokes")

# FortiGate NICs: wan ports on the ISP VLANs with a fixed MAC, other ports follow the per-port table
from app import provision_vms as pv  # noqa: E402
t = model.merged({"wanemu": {"uplink": LAB}}); cfg, w = t["pve"], t["wanemu"]
h = pv.fgt_nics(t["hubs"][0], cfg, w, True)
assert (h[0]["vlan"], h[0]["mac"]) == (3200, net.mac_for("site1-H1", 1)) and h[1]["vlan"] is None and not h[1].get("mac")
e = pv.fgt_nics(t["edges"][0], cfg, w, False)
assert [(n["vlan"], n["mac"]) for n in e[:2]] == [(3201, net.mac_for("site1-1", 1)), (3202, net.mac_for("site1-1", 2))]
spec = {"role": "fgt", "cores": 1, "memory_mb": 1, "extra_disks": [], "nics": e}
assert pv.vm_params(spec, dict(cfg, disk_storage="x"), "v", None)["net0"] == f"virtio={net.mac_for('site1-1', 1)},bridge=vmbr0,tag=3201"
assert pv.vm_params(spec, dict(cfg, disk_storage="x"), "v", None)["net2"] == "virtio,bridge=vmbr0"
s2 = pv.fgt_nics(t["edges"][1], cfg, w, False)                                       # site1-2: geen wan2
assert s2[0]["vlan"] == 3201 and s2[1].get("mac") is None and s2[1]["vlan"] is None
t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
specs = pv.build_specs(t, "pw", "pw", {d["name"]: "T" * 20 for d in t["hubs"] + t["edges"]}, "", "fgt")
assert specs[0]["nics"][0]["vlan"] == 3200 and specs[2]["nics"][1]["vlan"] == 3202
print("OK  FortiGate-NIC's: hub port1 -> ISP-HUB, spoke port1/port2 -> ISP-A/ISP-B met vaste MAC; LAN-poorten ongewijzigd")
assert net.ct_params(model.merged({"wanemu": {"uplink": LAB}}), 130, "k")["features"] == "nesting=1"     # Debian 13 / systemd 257 in a CT
print("OK  LXC: nesting=1 (anders falen /tmp, /run/lock en mqueue onder systemd 257)")
p = net.ct_params(model.merged({"wanemu": {"uplink": LAB}}), 130, "k")
assert p["nameserver"] == "1.1.1.1 8.8.8.8"   # not the PVE host's DNS (unreachable from the uplink VLAN)
print("OK  LXC: DNS uit de topologie (1.1.1.1 8.8.8.8), niet die van de PVE-host")

# FortiManager sees the real FortiGate addresses: no NAT towards the uplink subnet, routes on the FMG via wan-emu
t = model.merged({"wanemu": {"uplink": LAB}}); t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
at = net.agent_topology(t)
assert at["no_nat"] == ["10.10.106.0/24"], at
assert net.fmg_routes(t) == [("100.127.200.0", "255.255.255.0", "10.10.106.250"), ("100.127.201.0", "255.255.255.0", "10.10.106.250"),
                             ("100.127.202.0", "255.255.255.0", "10.10.106.250")]
t2 = model.merged({"wanemu": {"uplink": LAB}}); t2["fmgr"].update(ip="10.20.0.5/24", gateway="10.20.0.1")
assert net.fmg_routes(t2) == []                       # FMG not in the uplink subnet: the lab router must route
from app import bootstrap as bs  # noqa: E402
fl = bs.fmg_network_lines(t)
assert "edit 11" in fl and 'set dst 100.127.201.0 255.255.255.0' in fl and "set gateway 10.10.106.250" in fl
print("OK  FMG: geen NAT naar het lab-subnet, routes naar ISP-HUB/A/B via wan-emu (Day-0); niet als FMG elders staat")

# the CT template is fixed: a client cannot choose it
t = model.merged({"wanemu": {"template": "local:vztmpl/evil.tar.zst"}})
assert t["wanemu"]["template"] == model.DEFAULTS["wanemu"]["template"]
from app import csvio  # noqa: E402
assert "wanemu.template" not in csvio.export_csv(model.merged({"wanemu": {"uplink": LAB}}), full=True)
print("OK  CT-template vast (niet via API of CSV te kiezen)")

# no lab-specific uplink in the defaults: it must be filled in; an empty VLAN = untagged
d = model.merged({})
assert d["wanemu"]["uplink"] == {"vlan": None, "ip": "", "gw": ""}, d["wanemu"]["uplink"]
assert any("uplink: IP" in e for e in model.validate_wanemu(d)), model.validate_wanemu(d)
u = model.merged({"wanemu": {"uplink": {"vlan": None, "ip": "192.168.50.250/24", "gw": "192.168.50.1"}}})
assert model.validate_wanemu(u) == [], model.validate_wanemu(u)
assert net.ct_params(u, 130, "k")["net0"] == "name=eth0,bridge=vmbr0,ip=192.168.50.250/24,gw=192.168.50.1"
u["wanemu"]["uplink"]["vlan"] = 3201
assert any("unique" in e for e in model.validate_wanemu(u))
d["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
assert net.fmg_routes(d) == []                  # FortiManager-only run with the uplink not filled in: no crash
print("OK  uplink zonder lab-default; lege VLAN = untagged")
