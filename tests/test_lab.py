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

# ---- Task 2: inventory (roles, order, prefix, serial from the notes) and licence matching
from app import lab  # noqa: E402


class InvPve:
    """find_vms / find_cts / vm_config like Proxmox (cluster resources + notes)."""
    def __init__(self, vms, cts, notes):
        self.vms, self.cts, self.notes = vms, cts, notes
    def find_vms(self, tag): return self.vms
    def find_cts(self, tag): return self.cts
    def vm_config(self, node, vmid): return {"description": self.notes.get(vmid, "")}


VMS = [{"vmid": 111, "name": "SDW-fmg", "status": "running", "node": "pve01"},
       {"vmid": 113, "name": "SDW-site1-H1", "status": "running", "node": "pve01"},
       {"vmid": 115, "name": "SDW-site1-1", "status": "stopped", "node": "pve01"},
       {"vmid": 116, "name": "SDW-site1-2", "status": "running", "node": "pve01"},
       {"vmid": 200, "name": "SDW-other", "status": "running", "node": "pve02"}]          # other node: not this lab
CTS = [{"vmid": 112, "name": "SDW-wan-emu", "status": "running", "node": "pve01"},
       {"vmid": 118, "name": "SDW-client-site1-1", "status": "running", "node": "pve01"},
       {"vmid": 121, "name": "SDW-trafficgen", "status": "running", "node": "pve01"},
       {"vmid": 100, "name": "fgt-sdwan", "status": "running", "node": "pve01"}]           # the deploy container
NOTES = {111: "fgt-sdwan FortiManager ip=10.10.106.251 licence=FMVM1", 113: "fgt-sdwan licence=FGVMH1",
         115: "fgt-sdwan licence=FGVM11"}                                                   # site1-2: deployed before notes
assert lab.prefix_of(CTS) == "SDW-" and lab.prefix_of([]) == "" and lab.prefix_of([{"name": "wan-emu"}]) == ""
g = lab.inventory(InvPve(VMS, CTS, NOTES), "pve01", set())
by = {x["name"]: x for x in g}
assert set(by) == {"SDW-fmg", "SDW-site1-H1", "SDW-site1-1", "SDW-site1-2", "SDW-wan-emu", "SDW-client-site1-1", "SDW-trafficgen"}, set(by)
assert [by[n]["role"] for n in ("SDW-fmg", "SDW-site1-H1", "SDW-wan-emu", "SDW-client-site1-1", "SDW-trafficgen")] == ["fmg", "fgt", "wanemu", "client", "tgen"]
assert by["SDW-site1-1"]["device"] == "site1-1" and by["SDW-site1-1"]["serial"] == "FGVM11" and by["SDW-site1-2"]["serial"] == ""
assert by["SDW-site1-1"]["status"] == "stopped" and by["SDW-fmg"]["kind"] == "vm" and by["SDW-wan-emu"]["kind"] == "ct"
rank = {"client": 0, "tgen": 0, "fgt": 1, "wanemu": 2, "fmg": 3}
assert [rank[x["role"]] for x in g] == sorted(rank[x["role"]] for x in g), [x["role"] for x in g]    # Stop order
# FortiManager without notes: by the remembered logins' VM name, or by a device name starting with fmg
g2 = lab.inventory(InvPve([{"vmid": 130, "name": "SDW-mgr", "status": "running", "node": "pve01"},
                           {"vmid": 131, "name": "SDW-fmg2", "status": "running", "node": "pve01"}], CTS, {}), "pve01", {"SDW-mgr"})
assert {x["name"]: x["role"] for x in g2 if x["kind"] == "vm"} == {"SDW-mgr": "fmg", "SDW-fmg2": "fmg"}

ENTS = [{"serialNumber": "FMVM1", "description": "fgt-sdwan SDWAN fmg", "status": "ACTIVE"},
        {"serialNumber": "FGVMH1", "description": "fgt-sdwan SDWAN site1-H1", "status": "ACTIVE"},
        {"serialNumber": "FGVM11", "description": "fgt-sdwan SDWAN site1-1", "status": "STOPPED"},
        {"serialNumber": "FGVM12", "description": "fgt-sdwan SDWAN site1-2", "status": "ACTIVE"},
        {"serialNumber": "FGVM12B", "description": "fgt-sdwan SDWAN-MV site1-2", "status": "ACTIVE"},   # same name, other ADOM
        {"serialNumber": "FGVM99", "description": "fgt-sdwan SDWAN site1-9", "status": "ACTIVE"}]       # not in this lab
m = lab.match_licences(g, ENTS)
ms = {x["serial"]: x for x in m}
assert set(ms) == {"FMVM1", "FGVMH1", "FGVM11", "FGVM12", "FGVM12B"}, set(ms)
assert all(ms[s]["source"] == "notes" for s in ("FMVM1", "FGVMH1", "FGVM11"))
assert ms["FGVM12"]["source"] == ms["FGVM12B"]["source"] == "name" and ms["FGVM12"]["vm"] == "SDW-site1-2"
assert ms["FGVM11"]["status"] == "STOPPED" and ms["FGVMH1"]["device"] == "site1-H1"
# a serial in the notes FortiFlex no longer lists: still shown (status unknown), never matched by name instead
m = lab.match_licences(g, [e for e in ENTS if e["serialNumber"] != "FGVM11"])
assert {x["serial"]: x["status"] for x in m}["FGVM11"] == "" and not any(x["vm"] == "SDW-site1-1" and x["source"] == "name" for x in m)
print("OK  lab: inventaris (rollen, volgorde, prefix, serie uit notities) en licenties (notities, anders op naam)")

# the Cleanup licence lookup used to compare the full Proxmox name (with prefix) to the device in the description
from app import main  # noqa: E402
main._flex_cfg = lambda: (None, {"user": "u", "fmg_config": 1, "fgt_config": 2})
main._flex_client = lambda *a, **k: object()
main._tool_licenses = lambda cl, cfg: ENTS
main.db.put_secret(main.db.connect(main.DATA_DIR), "fortiflex_password", "x")
_, found = main._licences_for({"SDW-site1-H1", "SDW-fmg"})
assert sorted(e["serialNumber"] for e in found) == ["FGVMH1", "FMVM1"], found
print("OK  cleanup: licenties gevonden ook met een naamprefix")
