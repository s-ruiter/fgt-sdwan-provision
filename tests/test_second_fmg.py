"""A second FortiManager: the hostname is its identity (VM name + FortiFlex licence). The same name with another IP
would reuse the first VM and wait in vain for the new IP: stop before anything (licences, VMs) is touched."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model, provision_vms as pv  # noqa: E402
from app.pve import PveError  # noqa: E402

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def topo(name="fmg", ip="10.10.106.249/24", prefix="SDW-"):
    t = model.merged({})
    t["pve"]["name_prefix"] = prefix
    t["fmgr"].update(name=name, ip=ip, gateway="10.10.106.1")
    return t


class Pve:
    def __init__(self, vms):
        self.vms = vms                                  # {name: (vmid, description)}

    def find_vms(self, tag):
        return [{"name": n, "vmid": v[0]} for n, v in self.vms.items()]

    def vm_config(self, node, vmid):
        d = next(v[1] for v in self.vms.values() if v[0] == vmid)
        return {"description": d} if d is not None else {}


# a new FortiManager VM records its address in the Proxmox notes
spec = pv.build_specs(topo(), "pw", "pw", {}, "TOKEN12345ABCDEF", "fmg")[0]
p = pv.vm_params(spec, dict(topo()["pve"], disk_storage="x"), "v", None)
assert re.search(r"ip=10\.10\.106\.249/24", p.get("description", "")), p.get("description")

# same name, same IP: reuse (a re-run)
pv.check_fmg_reuse(Pve({"SDW-fmg": (111, pv.fmg_note("10.10.106.249/24"))}), topo())
# same name, other IP: stop, and say how to get a second one
try:
    pv.check_fmg_reuse(Pve({"SDW-fmg": (111, pv.fmg_note("10.10.106.11/24"))}), topo())
    raise SystemExit("not refused")
except PveError as e:
    m = str(e)
    assert "SDW-fmg" in m and "111" in m and "10.10.106.11" in m and "10.10.106.249" in m and "hostname" in m.lower(), m
# another hostname: a new VM, nothing to check
pv.check_fmg_reuse(Pve({"SDW-fmg": (111, pv.fmg_note("10.10.106.11/24"))}), topo(name="fmg2"))
# an older VM without notes: the address the tool remembered for that name, else nothing known (reuse as before)
try:
    pv.check_fmg_reuse(Pve({"SDW-fmg": (111, None)}), topo(), known={"fmg": "10.10.106.11"})
    raise SystemExit("not refused (remembered address)")
except PveError:
    pass
pv.check_fmg_reuse(Pve({"SDW-fmg": (111, None)}), topo(), known={"other": "10.10.106.11"})
pv.check_fmg_reuse(Pve({"SDW-fmg": (111, "")}), topo())
print("OK  tweede FortiManager: andere hostname = nieuwe VM; zelfde naam met ander IP stopt vooraf")

# the hostname is on the Simple page too (it is how you make a second FortiManager)
html = open(os.path.join(root, "static", "index.html")).read()
row = html[html.rindex("<div", 0, html.index('id="fm_name"')):html.index('id="fm_name"')]
assert " adv" not in row and "adv " not in row, row
print("OK  UI: FortiManager-hostname ook in Simple")

# the deploy request checks it before licences (only when it deploys the FortiManager VM itself)
import tempfile  # noqa: E402
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
from types import SimpleNamespace as NS  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app import main  # noqa: E402
fake = Pve({"SDW-fmg": (111, pv.fmg_note("10.10.106.11/24"))})
pv.make_pve = lambda cfg, secret: fake
rq = lambda scope, fmg="day0": NS(scope=scope, bootstrap=NS(fmg=fmg), secrets=NS(pve_token_secret="s"))
try:
    main._check_fmg_reuse(topo(), rq("fmg"))
    raise SystemExit("deploy not refused")
except HTTPException as e:
    assert e.status_code == 422 and "10.10.106.11" in str(e.detail), e.detail
for sc, m in (("fgt", "day0"), ("tpl", "day0"), ("fmg", "ssh")):
    main._check_fmg_reuse(topo(), rq(sc, m))                     # existing FortiManager / no VM: nothing to check
main._check_fmg_reuse(topo(name="fmg2"), rq("all"))
dep = open(os.path.join(root, "app", "main.py")).read()
dep = dep[dep.index("def deploy(r: DeployReq)"):]
assert dep.index("_check_fmg_reuse(topo, r)") < dep.index("lic = _licenses(topo, r, dry=False)")
print("OK  deploy: controle vóór de licenties (alleen bij een nieuwe FortiManager-VM)")
