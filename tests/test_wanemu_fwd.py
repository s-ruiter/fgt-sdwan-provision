"""WAN emulator extensions: port forwarding (DNAT), LAN VLANs per FortiGate port, a LAN client per spoke."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model  # noqa: E402
import labuplink  # noqa: E402,F401  (fills in the lab uplink: it has no default)
from app.wanemu import agent, net  # noqa: E402

t = model.merged({})
f = net.forwards(t)
assert [(x["name"], x["ip"], x["https"], x["ssh"]) for x in f] == [
    ("site1-H1", "100.64.1.2", 10443, 10022), ("site1-H2", "100.64.2.2", 10444, 10023),
    ("site1-1", "100.65.1.2", 10445, 10024), ("site1-2", "100.65.2.2", 10446, 10025),
    ("site1-3", "100.65.3.2", 10447, 10026)], f
at = net.agent_topology(t)
assert at["forwards"] == f
r = agent.nft_ruleset(at, {})
assert "type nat hook prerouting priority -100" in r
assert 'iifname "eth0" tcp dport 10443 dnat ip to 100.64.1.2:443' in r
assert 'iifname "eth0" tcp dport 10026 dnat ip to 100.65.3.2:22' in r
assert "dnat" not in agent.nft_ruleset(dict(at, forwards=[]), {})
assert model.validate_wanemu(t) == []
bad = model.merged({"wanemu": {"fwd_ssh_base": 10440}})                      # overlaps the HTTPS range
assert any("port" in e for e in model.validate_wanemu(bad)), model.validate_wanemu(bad)
bad = model.merged({"wanemu": {"fwd_https_base": 8085}})                       # 8085..8089 covers agent_port 8090? no
assert model.validate_wanemu(bad) == []
bad = model.merged({"wanemu": {"fwd_https_base": 8088}})                       # 8088..8092 contains the agent port
assert any("8090" in e for e in model.validate_wanemu(bad))
print("OK  port forwarding: HTTPS 10443+n en SSH 10022+n per FortiGate (hubs, dan spokes), DNAT op eth0, bereiken gevalideerd")

# LAN VLANs: LAN1 of FortiGate n on 3301+n, LAN2 on 3401+n; other ports keep the per-port table
from app import provision_vms as pv  # noqa: E402
t = model.merged({"region": dict(model.DEFAULTS["region"], multi_vrf=True)})     # multi-VRF: LAN1 + LAN2 per FortiGate
cfg, w = t["pve"], t["wanemu"]
cfg["ports"]["port4"] = {"bridge": "vmbr1", "vlan": 44}
h1 = pv.fgt_nics(t["hubs"][0], cfg, w, True, 0)
assert h1[4]["vlan"] == 3301 and h1[4]["bridge"] == "vmbr0" and h1[3] == {"bridge": "vmbr1", "vlan": 44}, h1
s1 = pv.fgt_nics(t["edges"][0], cfg, w, False, 2)                   # site1-1 = FortiGate n=2
assert (s1[4]["vlan"], s1[5]["vlan"]) == (3303, 3403), s1
assert net.lan_vlans(t)["site1-3"] == (3305, 3405) and net.lan_vlans(t)["site1-H2"][0] == 3302
sv = model.merged({})                                                 # single-VRF: no LAN2 NIC on port6
assert pv.fgt_nics(sv["edges"][0], sv["pve"], sv["wanemu"], False, 2)[5]["vlan"] is None
specs = pv.build_specs(dict(t, fmgr=dict(t["fmgr"], ip="10.10.106.11/24", gateway="10.10.106.1")), "pw", "pw",
                       {d["name"]: "T" * 20 for d in t["hubs"] + t["edges"]}, "", "fgt")
assert [s["nics"][4]["vlan"] for s in specs] == [3301, 3302, 3303, 3304, 3305]
bad = model.merged({"wanemu": {"lan_vlan_base": 3199}})              # 3200..3204 hits ISP-HUB/A/B
assert any("LAN VLAN" in e for e in model.validate_wanemu(bad)), model.validate_wanemu(bad)
print("OK  LAN-VLAN's: LAN1 3301+n, LAN2 3401+n per FortiGate; andere poorten volgen de poortentabel; botsing gevalideerd")

# LAN client per spoke: CT client-<spoke> on the LAN1 VLAN, .10 in the LAN, gateway = spoke LAN IP
t = model.merged({})
p = net.client_params(t, t["edges"][0], 140, "Geheim-123")
assert p["hostname"] == "client-site1-1" and p["vmid"] == 140 and p["password"] == "Geheim-123"
assert p["net0"] == "name=eth0,bridge=vmbr0,tag=3303,ip=10.0.1.10/24,gw=10.0.1.1", p["net0"]
assert p["nameserver"] == "1.1.1.1 8.8.8.8" and p["unprivileged"] == 1 and p["features"] == "nesting=1" and "fgt-sdwan" in p["tags"]
t2 = model.merged({}); t2["edges"][0]["lans"][0]["ip"] = "10.0.1.10/24"                 # spoke itself on .10 -> client .11
assert "ip=10.0.1.11/24,gw=10.0.1.10" in net.client_params(t2, t2["edges"][0], 1, "x")["net0"]

from app.pve import Pve  # noqa: E402
from app.wanemu import setup  # noqa: E402
from tests import fake_proxmox  # noqa: E402
fake_proxmox.reset()
srv = fake_proxmox.start(8091)
pve = Pve("http://127.0.0.1:8091", "root@pam!t", "secret", poll=0)
t = model.merged({"pve": {"node": "pve1"}})
ev = []
r = setup.deploy_clients(pve, t, "Geheim-123", lambda *a: ev.append(a))
cts = {c["params"]["hostname"]: c for c in fake_proxmox.STATE["cts"].values()}
assert set(cts) == {"client-site1-1", "client-site1-2", "client-site1-3"} and all(c["status"] == "running" for c in cts.values())
assert [x["name"] for x in r] == ["client-site1-1", "client-site1-2", "client-site1-3"] and all(x["created"] for x in r)
assert all("Geheim-123" not in m for _, _, m in ev)
t["edges"][1]["lans"][0]["ip"] = "10.0.22.1/24"                                           # changed LAN: reused, reconfigured
r2 = setup.deploy_clients(pve, t, "Geheim-123", lambda *a: ev.append(a))
assert len(fake_proxmox.STATE["cts"]) == 3 and not any(x["created"] for x in r2)
assert "ip=10.0.22.10/24,gw=10.0.22.1" in cts["client-site1-2"]["params"]["net0"]
srv.shutdown()
print("OK  LAN-clients: client-<spoke> op het LAN1-VLAN, .10 met de spoke als gateway; hergebruik met nieuwe netwerkconfig")

# review: dry run with a spoke without LAN does not crash the 'clients' step
from app import fmg as fmgmod  # noqa: E402
t = model.merged({}); t["edges"][1]["lans"] = []
ev = []
fmgmod._clients(t, {"dry": True}, lambda *a: ev.append(a))
assert [st for s_, st, _ in ev if s_ == "clients"][-1] == "ok" and not any("client-site1-2" in m for _, _, m in ev), ev
print("OK  LAN-clients dry-run: spoke zonder LAN wordt overgeslagen")

# forwarded connections get wan-emu's segment address as source: the FortiGate always answers on a connected subnet
# (a hub blackholes 10.0.0.0/8, so replies to a lab source like 10.10.106.1 were lost)
t = model.merged({})
r = agent.nft_ruleset(net.agent_topology(t), {})
assert 'ct status dnat oifname { "eth1", "eth2", "eth3", "eth4", "eth5", "eth6", "eth7", "eth8" } masquerade' in r, r
assert "ct status dnat" not in agent.nft_ruleset(dict(net.agent_topology(t), forwards=[]), {})
print("OK  port forwarding: bron van doorgestuurde verbindingen = wan-emu in het segment (antwoord altijd terug)")
