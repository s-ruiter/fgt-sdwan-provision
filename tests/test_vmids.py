"""VM ids of a deployment form one block: FortiManager, wan-emu, hubs, spokes, LAN clients, in that order."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model, provision_vms as pv  # noqa: E402
from app.pve import Pve  # noqa: E402
from tests import fake_proxmox as fp  # noqa: E402

fp.reset()
srv = fp.start(8084)
pve = Pve("http://127.0.0.1:8084", "root@pam!t", "secret", poll=0)
S = fp.STATE
for vmid in (100, 101, 105):                                   # someone else's VMs
    S["vms"][str(vmid)] = {"vmid": vmid, "name": f"other{vmid}", "params": {"tags": ""}, "status": "running"}
names = ["fmg", "wan-emu", "site1-H1", "site1-H2", "site1-1", "site1-2", "site1-3",
         "client-site1-1", "client-site1-2", "client-site1-3"]
plan = pv.plan_vmids(pve, names)
assert [plan[n] for n in names] == list(range(106, 116)), plan                  # first free block of 10 from 100
assert pv.plan_vmids(pve, names, first=200)["fmg"] == 200
S["vms"]["203"] = {"vmid": 203, "name": "x", "params": {"tags": ""}, "status": "stopped"}
assert pv.plan_vmids(pve, names, first=200)["fmg"] == 204                       # 200-209 is not free: next block
S["vms"]["300"] = {"vmid": 300, "name": "fmg", "params": {"tags": "fgt-sdwan"}, "status": "running"}   # reused: keeps its id
p2 = pv.plan_vmids(pve, names)
assert "fmg" not in p2 and [p2[n] for n in names[1:]] == list(range(106, 115)), p2
S["cts"]["301"] = {"vmid": 301, "params": {"hostname": "wan-emu", "tags": "fgt-sdwan"}, "status": "running"}
assert "wan-emu" not in pv.plan_vmids(pve, names) and pv.plan_vmids(pve, []) == {}
srv.shutdown()
assert pv.deploy_names(model.merged({}), "all") == names
assert pv.deploy_names(model.merged({}), "fgt") == names[1:]
assert pv.deploy_names(model.merged({}), "lan") == names[-3:]
assert pv.deploy_names(model.merged({}), "fmg") == ["fmg"] and pv.deploy_names(model.merged({}), "tpl") == []
print("OK  VM-ID's: één aaneengesloten blok (FMG, wan-emu, hubs, spokes, clients), vanaf 'first' of 100; hergebruik houdt zijn ID")
