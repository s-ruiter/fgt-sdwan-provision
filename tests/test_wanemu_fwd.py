"""WAN emulator extensions: port forwarding (DNAT), LAN VLANs per FortiGate port, a LAN client per spoke."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model  # noqa: E402
from app.wanemu import agent, net  # noqa: E402

t = model.merged({})
f = net.forwards(t)
assert [(x["name"], x["ip"], x["https"], x["ssh"]) for x in f] == [
    ("site1-H1", "100.127.200.11", 10443, 10022), ("site1-H2", "100.127.200.12", 10444, 10023),
    ("site1-1", "100.127.201.11", 10445, 10024), ("site1-2", "100.127.201.12", 10446, 10025),
    ("site1-3", "100.127.201.13", 10447, 10026)], f
at = net.agent_topology(t)
assert at["forwards"] == f
r = agent.nft_ruleset(at, {})
assert "type nat hook prerouting priority -100" in r
assert 'iifname "eth0" tcp dport 10443 dnat ip to 100.127.200.11:443' in r
assert 'iifname "eth0" tcp dport 10026 dnat ip to 100.127.201.13:22' in r
assert "dnat" not in agent.nft_ruleset(dict(at, forwards=[]), {})
assert model.validate_wanemu(t) == []
bad = model.merged({"wanemu": {"fwd_ssh_base": 10440}})                      # overlaps the HTTPS range
assert any("poort" in e for e in model.validate_wanemu(bad)), model.validate_wanemu(bad)
bad = model.merged({"wanemu": {"fwd_https_base": 8085}})                       # 8085..8089 covers agent_port 8090? no
assert model.validate_wanemu(bad) == []
bad = model.merged({"wanemu": {"fwd_https_base": 8088}})                       # 8088..8092 contains the agent port
assert any("8090" in e for e in model.validate_wanemu(bad))
print("OK  port forwarding: HTTPS 10443+n en SSH 10022+n per FortiGate (hubs, dan spokes), DNAT op eth0, bereiken gevalideerd")

# LAN VLANs: LAN1 of FortiGate n on 3301+n, LAN2 on 3401+n; other ports keep the per-port table
from app import provision_vms as pv  # noqa: E402
t = model.merged({})
cfg, w = t["pve"], t["wanemu"]
cfg["ports"]["port4"] = {"bridge": "vmbr1", "vlan": 44}
h1 = pv.fgt_nics(t["hubs"][0], cfg, w, True, 0)
assert h1[4]["vlan"] == 3301 and h1[4]["bridge"] == "vmbr0" and h1[3] == {"bridge": "vmbr1", "vlan": 44}, h1
s1 = pv.fgt_nics(t["edges"][0], cfg, w, False, 2)                   # site1-1 = FortiGate n=2
assert (s1[4]["vlan"], s1[5]["vlan"]) == (3303, 3403), s1
assert net.lan_vlans(t)["site1-3"] == (3305, 3405) and net.lan_vlans(t)["site1-H2"][0] == 3302
specs = pv.build_specs(dict(t, fmgr=dict(t["fmgr"], ip="10.10.106.11/24", gateway="10.10.106.1")), "pw", "pw",
                       {d["name"]: "T" * 20 for d in t["hubs"] + t["edges"]}, "", "fgt")
assert [s["nics"][4]["vlan"] for s in specs] == [3301, 3302, 3303, 3304, 3305]
bad = model.merged({"wanemu": {"lan_vlan_base": 3199}})              # 3200..3204 hits ISP-HUB/A/B
assert any("LAN-VLAN" in e for e in model.validate_wanemu(bad)), model.validate_wanemu(bad)
print("OK  LAN-VLAN's: LAN1 3301+n, LAN2 3401+n per FortiGate; andere poorten volgen de poortentabel; botsing gevalideerd")
