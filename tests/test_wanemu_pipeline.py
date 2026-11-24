"""WAN-emulator deployment: LXC via (fake) Proxmox, provisioning via a (fake) SSH shell, reuse, rollback, checks."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model  # noqa: E402
import labuplink  # noqa: E402,F401  (fills in the lab uplink: it has no default)
from app.pve import Pve  # noqa: E402
from app.wanemu import setup  # noqa: E402
from tests import fake_proxmox  # noqa: E402
from tests.fake_wanemu import FakeShell, connector  # noqa: E402

fake_proxmox.reset()
srv = fake_proxmox.start(8098)
pve = Pve("http://127.0.0.1:8098", "root@pam!t", "secret", poll=0)
key = setup.new_ssh_key()
assert key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") and setup.public_key(key).startswith("ssh-ed25519 ")
SEC = {"ssh_key": key, "agent_token": "agenttoken1234567890"}
t = model.merged({"pve": {"node": "pve1"}})
ev = []
emit = lambda step, st, msg: ev.append((step, st, msg))
waiter = lambda h, p, up, tmo: True

# 1. new container
sh = FakeShell()
con = connector(sh)
r = setup.deploy_wanemu(pve, t, SEC, emit, connector=con, waiter=waiter)
assert r["created"] is True and r["internet"] is True and r["vmid"] in (100, 101)
ct = fake_proxmox.STATE["cts"][str(r["vmid"])]
assert ct["status"] == "running" and ct["params"]["net3"].startswith("name=eth3,bridge=vmbr0,tag=3211,ip=100.65.1.1/24,firewall=0,hwaddr=")
assert ct["params"]["ssh-public-keys"] == setup.public_key(key) and con.seen[0][0] == "10.10.106.250"
assert "apt-get install -y -q dnsmasq nftables iproute2 python3" in " ".join(sh.cmds)
assert "dhcp-host=" in sh.files["/etc/dnsmasq.d/wan-emu.conf"][0]
assert sh.files["/etc/wan-emu/token"] == ("agenttoken1234567890", 0o600)
assert '"links"' in sh.files["/etc/wan-emu/topology.json"][0] and "def tc_commands" in sh.files["/opt/wan-emu/agent.py"][0]
unit = sh.files["/etc/systemd/system/wan-emu-agent.service"][0]
assert "--listen 10.10.106.250 --port 8090" in unit and "agenttoken" not in unit
assert any("systemctl restart wan-emu-agent" in c for c in sh.cmds) and any("ip_forward" in f[0] for f in sh.files.values())
assert all("agenttoken" not in c for c in sh.cmds) and all("agenttoken" not in m and "PRIVATE" not in m for _, _, m in ev)
print("OK  nieuwe CT: aangemaakt met ISP-VLAN's, gestart, pakketten, dnsmasq, token 0600, agent-service, forwarding")

# 2. reuse with changed settings: same CT, new config applied, provisioned again
t2 = model.merged({"pve": {"node": "pve1"}, "wanemu": {"uplink": {"ip": "10.10.106.249/24"}}})
sh2 = FakeShell(installed=True)
r2 = setup.deploy_wanemu(pve, t2, SEC, emit, connector=connector(sh2), waiter=waiter)
assert r2["created"] is False and r2["vmid"] == r["vmid"] and len(fake_proxmox.STATE["cts"]) == 1
assert fake_proxmox.STATE["cts"][str(r["vmid"])]["params"]["net0"].endswith("ip=10.10.106.249/24,gw=10.10.106.1")
assert not any("apt-get" in c for c in sh2.cmds) and "/etc/dnsmasq.d/wan-emu.conf" in sh2.files
print("OK  bestaande CT: hergebruikt, nieuwe netwerkconfig toegepast, opnieuw ingericht (geen apt als alles er is)")

# 3. no internet from the segments: warning, no failure
ev.clear()
r3 = setup.deploy_wanemu(pve, t, SEC, emit, connector=connector(FakeShell(installed=True, fail=("ping",))), waiter=waiter)
assert r3["internet"] is False and any(st == "warn" and "no internet from ISP-A" in m for _, st, m in ev)
print("OK  geen internet vanuit ISP-A: waarschuwing, uitrol gaat door")

# 4. netem/ifb missing on the host: clear error; a CT that existed is kept
try:
    setup.deploy_wanemu(pve, t, SEC, emit, connector=connector(FakeShell(installed=True, fail=("ifb-test",))), waiter=waiter)
    raise SystemExit("verwachtte fout")
except setup.WanEmuError as e:
    assert "sch_netem" in str(e) and "ifb" in str(e)
assert str(r["vmid"]) in fake_proxmox.STATE["cts"]
# 5. failure on a NEW container: removed again
fake_proxmox.reset()
try:
    setup.deploy_wanemu(pve, t, SEC, emit, connector=connector(FakeShell(fail=("apt-get",))), waiter=waiter)
    raise SystemExit("verwachtte fout")
except setup.WanEmuError:
    pass
assert fake_proxmox.STATE["cts"] == {}
# 6. SSH never comes up
fake_proxmox.reset()
try:
    setup.deploy_wanemu(pve, t, SEC, emit, connector=connector(FakeShell()), waiter=lambda h, p, up, tmo: False)
    raise SystemExit("verwachtte fout")
except setup.WanEmuError as e:
    assert "10.10.106.250" in str(e)
assert fake_proxmox.STATE["cts"] == {}
srv.shutdown()
print("OK  fouten: netem/ifb ontbreekt (duidelijke melding, bestaande CT blijft), nieuwe CT bij fout verwijderd, SSH-timeout")

# ---- review fixes
fake_proxmox.reset()
srv = fake_proxmox.start(8099)
pve = Pve("http://127.0.0.1:8099", "root@pam!t", "secret", poll=0)
sh = FakeShell(installed=True)
setup.deploy_wanemu(pve, t, SEC, emit, connector=connector(sh), waiter=waiter)
unit = sh.files["/etc/systemd/system/wan-emu-agent.service"][0]
assert "ExecStartPre=-/usr/sbin/sysctl -q -p /etc/sysctl.d/90-wan-emu.conf" in unit            # forwarding na reboot
assert any(c == "sysctl -q -p /etc/sysctl.d/90-wan-emu.conf" for c in sh.cmds) and not any("--system" in c for c in sh.cmds)
i_agent = next(i for i, c in enumerate(sh.cmds) if "ss -ltn" in c)
assert i_agent < next(i for i, c in enumerate(sh.cmds) if c.startswith("ping"))                # agent (NAT) vóór de ping
try:                                                                                          # agent komt niet op
    setup.deploy_wanemu(pve, t, SEC, emit, connector=connector(FakeShell(installed=True, fail=("ss -ltn",))), waiter=waiter)
    raise SystemExit("verwachtte fout")
except setup.WanEmuError as e:
    assert "agent" in str(e) and "journalctl -u wan-emu-agent" in str(e)
ev.clear()                                                                                    # VMID gewijzigd bij hergebruik
t3 = model.merged({"pve": {"node": "pve1"}, "wanemu": {"vmid": 555}})
setup.deploy_wanemu(pve, t3, SEC, emit, connector=connector(FakeShell(installed=True)), waiter=waiter)
assert any("CT id 555" in m and "delete" in m for _, _, m in ev), ev
# step status: no internet must stay a warning (not overwritten by ok)
from app import fmg as fmgmod  # noqa: E402
ev.clear()
fmgmod._wanemu(t, {"pve_client": pve, "wanemu_secrets": SEC, "waiter": waiter,
                   "wanemu_connector": connector(FakeShell(installed=True, fail=("ping",)))}, emit)
assert [st for s_, st, _ in ev if s_ == "pve_wan" and st != "info"][-1] == "warn", ev
srv.shutdown()
# validation: the emulator is only checked when FortiGates are deployed with Day-0
pv2 = model.merged({"pve": {"url": "https://pve:8006", "token_id": "a@pam!b", "node": "n", "disk_storage": "d",
                            "import_storage": "i", "iso_storage": "s"}})
pv2["wanemu"]["segments"]["hub"]["block"] = "nope"
assert not any("ISP-HUB" in e for e in model.validate_pve(pv2, wanemu=False))
assert any("ISP-HUB" in e for e in model.validate_pve(pv2))
print("OK  review: forwarding na herstart, sysctl alleen eigen bestand, agent-check vóór ping, VMID-melding, warn blijft staan, validatie alleen bij FortiGate-VM's")

# routes on an existing FortiManager via the API (pve_wan step), idempotent; a refusal is a warning with the CLI
import http.server  # noqa: E402
from app.fmg import Fmg  # noqa: E402
from tests import mock_fmg  # noqa: E402
msrv = http.server.HTTPServer(("127.0.0.1", 8093), mock_fmg.H)
import threading as _t  # noqa: E402
_t.Thread(target=msrv.serve_forever, daemon=True).start()
fake_proxmox.reset()
srv = fake_proxmox.start(8094)
pve = Pve("http://127.0.0.1:8094", "root@pam!t", "secret", poll=0)
tf = model.merged({"pve": {"node": "pve1"}}); tf["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
f = Fmg("http://127.0.0.1:8093", "admin", "x"); f.login()
b = {"pve_client": pve, "wanemu_secrets": SEC, "waiter": waiter, "wanemu_connector": connector(FakeShell(installed=True)), "fmg": f}
ev.clear()
fmgmod._wanemu(tf, b, emit)
rt = mock_fmg.STATE["routes"]
assert sorted((r["dst"][0], r["gateway"], r["device"]) for r in rt) == [("100.64.0.0", "10.10.106.250", "port1"),
       ("100.65.0.0", "10.10.106.250", "port1"), ("100.66.0.0", "10.10.106.250", "port1")], rt
n = len(rt)
fmgmod._wanemu(tf, b, emit)
assert len(mock_fmg.STATE["routes"]) == n                                   # second run: nothing added
mock_fmg.STATE["routes"].clear(); mock_fmg.STATE["refuse_route"] = True
ev.clear()
fmgmod._wanemu(tf, b, emit)
assert any(st == "warn" and "config system route" in m for s_, st, m in ev if s_ == "pve_wan"), ev
mock_fmg.STATE["refuse_route"] = False
srv.shutdown(); msrv.shutdown()
print("OK  FMG-routes via de API: toegevoegd, herhaalbaar, bij weigering een waarschuwing met de CLI")
