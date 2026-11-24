"""Name prefix for the Proxmox VMs and containers of a deployment (empty by default). Only the Proxmox names change:
FortiGate hostnames and FortiManager device names stay. A deployment made without (or with another) prefix is found
under its old name, renamed and reused."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model, provision_vms as pv, trafficgen as tg  # noqa: E402
from app.pve import Pve  # noqa: E402
from app.wanemu import net, setup  # noqa: E402
from tests import fake_proxmox as fp, fake_wanemu  # noqa: E402

LAB = {"vlan": 106, "ip": "10.10.106.250/24", "gw": "10.10.106.1"}

# ---- model: empty by default, a '-' is added, validated
assert model.merged({})["pve"]["name_prefix"] == ""
assert model.merged({"pve": {"name_prefix": " lab1 "}})["pve"]["name_prefix"] == "lab1-"
assert model.merged({"pve": {"name_prefix": "lab1-"}})["pve"]["name_prefix"] == "lab1-"
t = model.merged({"pve": {"name_prefix": "lab1"}})
assert model.pve_name(t, "site1-H1") == "lab1-site1-H1" and model.pve_name(model.merged({}), "fmg") == "fmg"
for bad in ("a b", "x_y", "-x", "a" * 30):
    b = model.merged({"pve": {"name_prefix": bad}})
    b["pve"].update(url="https://pve:8006", token_id="a@pam!b", node="n", disk_storage="d", import_storage="i", iso_storage="s")
    assert any("Name prefix" in e for e in model.validate_pve(b, wanemu=False)), bad
print("OK  prefix: standaard leeg, '-' erachter, alleen letters/cijfers/'-' (max 20)")

# ---- names of a deployment (id block) carry the prefix; device names do not change
t = model.merged({"pve": {"name_prefix": "lab1"}, "wanemu": {"uplink": LAB},
                  "trafficgen": {"enabled": True, "ip": "10.10.106.249/24"}})
t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
names = pv.deploy_names(t, "all")
assert names[0] == "lab1-fmg" and "lab1-wan-emu" in names and "lab1-site1-H1" in names and names[-1] == "lab1-trafficgen", names
assert all(n.startswith("lab1-") for n in names)
assert [d["name"] for d in t["hubs"]] == ["site1-H1", "site1-H2"]
specs = pv.build_specs(t, "pw", "pw", {}, None, "fgt")
assert specs[0]["name"] == "site1-H1" and specs[0]["pve_name"] == "lab1-site1-H1"
assert net.ct_params(t, 130, "k")["hostname"] == "lab1-wan-emu"
assert net.client_params(t, t["edges"][0], 140, "x")["hostname"] == "lab1-client-site1-1"
assert tg.ct_params(t, 150, "k", "x")["hostname"] == "lab1-trafficgen"
print("OK  namen: VM's en CT's met prefix (id-blok ook), FortiGate-namen ongewijzigd")

# ---- Proxmox: create with prefix; an old deployment without prefix is renamed and reused (same id)
fp.reset()
srv = fp.start(8086)
try:
    pve = Pve("http://127.0.0.1:8086", "root@pam!t", "secret", poll=0)
    S = fp.STATE
    S["vms"]["120"] = {"vmid": 120, "name": "site1-H1", "params": {"tags": "fgt-sdwan"}, "status": "running"}   # old, no prefix
    S["cts"]["121"] = {"vmid": 121, "params": {"hostname": "wan-emu", "tags": "fgt-sdwan"}, "status": "running"}
    S["cts"]["122"] = {"vmid": 122, "params": {"hostname": "client-site1-1", "tags": "fgt-sdwan"}, "status": "stopped"}
    plan = pv.plan_vmids(pve, pv.deploy_names(t, "all"), prefix="lab1-")
    assert "lab1-site1-H1" not in plan and "lab1-wan-emu" not in plan and "lab1-fmg" in plan, plan    # reused: no new id
    ev = []
    emit = lambda s, st, m: ev.append(m)
    hub = [s for s in specs if s["name"] == "site1-H1"]
    pv.deploy_vms(pve, t["pve"] | {"node": "pve1"}, hub, {}, emit, "pve_fgt", st := {}, start=False)
    assert S["vms"]["120"]["name"] == "lab1-site1-H1" and st["site1-H1"]["vmid"] == 120 and len(S["vms"]) == 1
    assert any("renamed" in m and "lab1-site1-H1" in m for m in ev), ev
    # wan-emu: found under the old hostname, its hostname updated (renamed) in place
    t["pve"]["node"] = "pve1"
    sh = fake_wanemu.FakeShell(installed=True)
    r = setup.deploy_wanemu(pve, t, {"ssh_key": setup.new_ssh_key(), "agent_token": "tok1234567890abcdef"}, emit,
                            connector=fake_wanemu.connector(sh), waiter=lambda *a: True)
    assert r["vmid"] == 121 and not r["created"] and S["cts"]["121"]["params"]["hostname"] == "lab1-wan-emu"
    # clients: client-site1-1 renamed + reused, the others created with the prefix
    out = setup.deploy_clients(pve, t, "Client-pw1", emit)
    assert {o["name"]: o["created"] for o in out} == {"lab1-client-site1-1": False, "lab1-client-site1-2": True,
                                                       "lab1-client-site1-3": True}, out
    assert S["cts"]["122"]["params"]["hostname"] == "lab1-client-site1-1"
    hosts = sorted(c["params"]["hostname"] for c in S["cts"].values())
    assert hosts == ["lab1-client-site1-1", "lab1-client-site1-2", "lab1-client-site1-3", "lab1-wan-emu"], hosts
finally:
    srv.shutdown()
print("OK  Proxmox: nieuw met prefix; oude uitrol (zonder prefix) gevonden, hernoemd en hergebruikt (zelfde id)")
print("alle prefix-tests geslaagd")
