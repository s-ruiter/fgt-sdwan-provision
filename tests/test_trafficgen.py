"""Traffic-Gen (optional): a container with management on the WAN emulator uplink (lab) and test traffic as clients in
the LAN of the first spoke (.20-.29, the spoke as gateway). Model, validation, Proxmox params, provisioning over SSH."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model, trafficgen as tg  # noqa: E402
from app import provision_vms as pv  # noqa: E402
from app.pve import Pve  # noqa: E402
from app.wanemu import net  # noqa: E402
from app.wanemu.setup import new_ssh_key, public_key  # noqa: E402
from tests import fake_proxmox, fake_wanemu  # noqa: E402

LAB = {"vlan": 106, "ip": "10.10.106.250/24", "gw": "10.10.106.1"}


def topo(**tgk):
    t = model.merged({"wanemu": {"uplink": LAB}, "trafficgen": dict({"enabled": True, "ip": "10.10.106.249/24"}, **tgk)})
    t["pve"]["node"] = "pve1"
    return t


# ---- model + validation
d = model.merged({})
assert d["trafficgen"]["enabled"] is True and d["trafficgen"]["count"] == 10, d["trafficgen"]   # always comes along
off = model.merged({"trafficgen": {"enabled": False}})
assert tg.validate(off) == []                                                     # off (old settings): nothing to check
t = topo()
assert tg.validate(t) == [], tg.validate(t)
assert tg.spoke(t)["name"] == "site1-1"                                           # the first spoke with a LAN
assert topo(ip="")["trafficgen"]["ip"] == "10.10.106.249/24"                     # empty: derived from the uplink
for bad, why in (({"ip": "10.10.107.5/24"}, "uplink"), ({"ip": "10.10.106.250/24"}, "wan-emu"),
                 ({"ip": "10.10.106.1/24"}, "gateway")):
    e = tg.validate(topo(**bad))
    assert any(why in x for x in e), (bad, e)
t2 = topo(); t2["edges"][0]["lans"][0]["ip"] = "10.0.1.25/24"                     # spoke LAN address inside .20-.29
assert any("10.0.1.25" in x for x in tg.validate(t2)), tg.validate(t2)
t3 = topo(); t3["edges"] = [dict(e, lans=[]) for e in t3["edges"]]
assert any("spoke" in x for x in tg.validate(t3)), tg.validate(t3)
t4 = topo(); t4["wanemu"]["uplink"] = {"bridge": "", "vlan": None, "ip": "", "gw": ""}
assert any("uplink" in x for x in tg.validate(t4)), tg.validate(t4)
print("OK  model/validatie: standaard aan (IP = uplink min één); IP in het uplink-netwerk (niet wan-emu/gateway), spoke met LAN, .20-.29 vrij")

# ---- Proxmox params, Traffic-Gen config, id block, bridges
t = topo()
key = new_ssh_key()
p = tg.ct_params(t, 140, public_key(key), "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst")
assert p["hostname"] == "trafficgen" and p["vmid"] == 140 and "fgt-sdwan" in p["tags"] and p["unprivileged"] == 1
assert p["features"] == "nesting=1" and p["ssh-public-keys"].startswith("ssh-ed25519 ") and "password" not in p
assert p["net0"] == "name=eth0,bridge=vmbr0,tag=106,ip=10.10.106.249/24,gw=10.10.106.1,firewall=0", p["net0"]
lan1 = net.lan_vlans(t)["site1-1"][0]
assert p["net1"] == f"name=eth1,bridge=vmbr0,tag={lan1},firewall=0", p["net1"]
assert p["ostemplate"].startswith("local:vztmpl/debian-12-standard") and p["nameserver"] == "1.1.1.1 8.8.8.8"
u = topo(); u["wanemu"]["bridge"] = "vmbr5"; u["wanemu"]["uplink"] = dict(LAB, bridge="vmbr0", vlan=None)
pu = tg.ct_params(u, 140, "k", "x")
assert pu["net0"].startswith("name=eth0,bridge=vmbr0,ip=") and pu["net1"].startswith("name=eth1,bridge=vmbr5,tag=")
c = tg.config(t)
assert c == {"mgmt_interface": "eth0", "networks": [{"name": "site1-1-lan1", "parent": "eth1", "vlan": None,
             "first": "10.0.1.20", "prefix": 24, "gateway": "10.0.1.1", "count": 10, "dns": "1.1.1.1"}]}, c
assert pv.deploy_names(t, "all")[-1] == "trafficgen" and pv.deploy_names(t, "lan") [-1] == "trafficgen"
assert "trafficgen" not in pv.deploy_names(model.merged({"trafficgen": {"enabled": False}}), "all")
assert "trafficgen" not in pv.deploy_names(t, "fmg")
need = net.bridge_needs(t, fgts=False, clients=True)
assert 106 in need["vmbr0"] and lan1 in need["vmbr0"]                            # LAN clients only + Traffic-Gen
print("OK  CT: eth0 = uplink-bridge/VLAN + eigen IP, eth1 = LAN1-VLAN spoke 1, sleutel; config .20-.29 via de spoke")

# ---- deploy over SSH (fake Proxmox + fake shell)
fake_proxmox.reset()
srv = fake_proxmox.start(8098)
try:
    pve = Pve("http://127.0.0.1:8098", "root@pam!t", "secret")
    pve.poll = 0.01
    ev = []
    emit = lambda s, st, m: ev.append((s, st, m))
    sh = fake_wanemu.FakeShell()
    con = fake_wanemu.connector(sh)
    r = tg.deploy(pve, t, key, "Tg-Admin-pw1!", emit, connector=con, waiter=lambda h, p_, up, tmo: True, vmids={"trafficgen": 141})
    assert r == {"vmid": 141, "created": True, "url": "https://10.10.106.249:8443"}, r
    ct = fake_proxmox.STATE["cts"]["141"]["params"]
    assert ct["hostname"] == "trafficgen" and ct["ostemplate"].startswith("local:vztmpl/debian-12-standard_")
    assert con.seen == [("10.10.106.249", key)]
    run = "\n".join(sh.cmds)
    assert "apt-get install" in run and "github.com/s-ruiter/Traffic-Gen/archive/" in run and "trafficgen.py passwd --file" in run
    assert "systemctl enable" in run and ":8443" in run and "Tg-Admin-pw1!" not in run                # password only as a file
    assert sh.files["/root/.tg-pw"] == ("Tg-Admin-pw1!", 0o600) and "rm -f /root/.tg-pw" in run
    assert "site1-1-lan1" in sh.files["/root/.tg-networks.json"][0]
    assert "TRAFFICGEN_PORT=8443" in sh.files["/etc/systemd/system/trafficgen-web.service"][0]
    # second run: the same container is reused and updated, not created again
    sh2 = fake_wanemu.FakeShell(installed=True)
    r2 = tg.deploy(pve, t, key, "Tg-Admin-pw1!", emit, connector=fake_wanemu.connector(sh2), waiter=lambda *a: True)
    assert r2["vmid"] == 141 and r2["created"] is False and len(fake_proxmox.STATE["cts"]) == 1
    assert not any("apt-get install" in x for x in sh2.cmds)                        # packages already there
    # a new container whose provisioning fails is removed again
    fake_proxmox.reset()
    bad = fake_wanemu.FakeShell(fail=("trafficgen.py passwd",))
    try:
        tg.deploy(pve, t, key, "Tg-Admin-pw1!", emit, connector=fake_wanemu.connector(bad), waiter=lambda *a: True, vmids={"trafficgen": 142})
        raise AssertionError("expected TrafficGenError")
    except tg.TrafficGenError as e:
        assert "passwd" in str(e)
    assert not fake_proxmox.STATE["cts"]
    # not reachable over SSH: clear message about the management network
    fake_proxmox.reset()
    try:
        tg.deploy(pve, t, key, "Tg-Admin-pw1!", emit, connector=fake_wanemu.connector(fake_wanemu.FakeShell()),
                  waiter=lambda *a: False, vmids={"trafficgen": 143})
        raise AssertionError("expected TrafficGenError")
    except tg.TrafficGenError as e:
        assert "10.10.106.249" in str(e) and "106" in str(e)
finally:
    srv.shutdown()
print("OK  uitrol: CT gemaakt (Debian 12), via SSH geïnstalleerd, wachtwoord alleen als bestand, hergebruik, opruimen bij fout")
print("alle Traffic-Gen-tests geslaagd")
