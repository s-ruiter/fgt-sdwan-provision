"""Lab start / stop: serial in the VM notes, inventory, licence matching, stop/start flows, endpoints.
python -m tests.test_lab"""
import inspect, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
from app import provision_vms as pv  # noqa: E402

# ---- Task 1: the FortiFlex serial goes into every VM's Proxmox notes at deployment
n = pv.vm_note("fmg", "10.0.0.5/24", "FMVMMLTM26004819")
assert pv.re.search(r"fgt-sdwan FortiManager ip=([0-9.]+)", n).group(1) == "10.0.0.5", n     # check_fmg_reuse still reads it
assert pv.note_serial(n) == "FMVMMLTM26004819", n
assert pv.vm_note("fgt", "", "FGVMMLTM26030827") == "fgt-sdwan licence=FGVMMLTM26030827"
assert pv.vm_note("fgt", "", "") == "" and pv.note_serial("") == "" and pv.note_serial("fgt-sdwan FortiManager ip=1.2.3.4") == ""
cfg = {"cpu_type": "host", "disk_storage": "local-lvm"}
base = {"cores": 2, "memory_mb": 2048, "extra_disks": [], "nics": []}
p = pv.vm_params(dict(base, role="fgt", serial="FGVMMLTM26030827"), cfg, "local:import/x.qcow2", None)
assert p["description"] == "fgt-sdwan licence=FGVMMLTM26030827", p.get("description")
assert "description" not in pv.vm_params(dict(base, role="fgt", serial=""), cfg, "local:import/x.qcow2", None)
p = pv.vm_params(dict(base, role="fmg", ip="10.0.0.5/24", serial="FMVM1"), cfg, "local:import/x.qcow2", None)
assert pv.note_serial(p["description"]) == "FMVM1" and "ip=10.0.0.5" in p["description"]


class NotePve:
    def __init__(self): self.calls = []
    def set_config(self, node, vmid, params): self.calls.append((node, vmid, params))


fp = NotePve()
pv.refresh_note(fp, "pve01", 115, {"role": "fgt", "serial": "FGVM9"})
assert fp.calls == [("pve01", 115, {"description": "fgt-sdwan licence=FGVM9"})], fp.calls
pv.refresh_note(fp, "pve01", 116, {"role": "fgt", "serial": ""})                        # nothing known: notes left alone
assert len(fp.calls) == 1
assert "refresh_note(" in inspect.getsource(pv.deploy_vms), "a reused VM gets its notes rewritten"
_t = __import__("app.model", fromlist=["x"]).merged({})
_t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1", serial="FMVM1")
specs = pv.build_specs(_t, "pw1", "pw2", {}, None, "fmg")
assert specs[0]["serial"] == "FMVM1", specs[0].get("serial")
print("OK  lab: serienummer in de VM-notities (nieuw en hergebruikt)")
