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
