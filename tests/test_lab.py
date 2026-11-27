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

# ---- Task 3: stop and start flows
import threading  # noqa: E402
from app import cancel  # noqa: E402
from app.pve import PveError  # noqa: E402


class PowerPve:
    """Records power calls; `stubborn` guests ignore a graceful shutdown (the task fails), `broken` fail to start."""
    def __init__(self, status, stubborn=(), broken=()):
        self.st, self.stubborn, self.broken, self.calls = dict(status), set(stubborn), set(broken), []
    def _status(self, vmid): return self.st[vmid]
    vm_status = ct_status = lambda self, node, vmid: self.st[vmid]
    def _sd(self, kind, vmid, timeout):
        self.calls.append(("shutdown", vmid, timeout))
        return ("fail" if vmid in self.stubborn else "ok", "off", vmid)
    def shutdown_vm(self, node, vmid, timeout): return self._sd("vm", vmid, timeout)
    def shutdown_ct(self, node, vmid, timeout): return self._sd("ct", vmid, timeout)
    def stop_vm(self, node, vmid): self.calls.append(("stop", vmid)); return ("ok", "off", vmid)
    stop_ct = stop_vm
    def start_vm(self, node, vmid):
        self.calls.append(("start", vmid))
        return ("fail" if vmid in self.broken else "ok", "on", vmid)
    start_ct = start_vm
    def wait_task(self, node, upid, timeout=3600):
        res, to, vmid = upid
        if res == "fail":
            raise PveError("task failed: guest did not stop" if to == "off" else "task failed: start")
        self.st[vmid] = "stopped" if to == "off" else "running"


class Flex:
    def __init__(self, fail=()): self.calls, self.fail = [], set(fail)
    def stop(self, sn): self.calls.append(("stop", sn))
    def reactivate(self, sn):
        self.calls.append(("reactivate", sn))
        if sn in self.fail:
            raise RuntimeError("FortiFlex said no")
    def create_vm(self, *a, **k): raise AssertionError("never a new licence")
    def regen_token(self, *a, **k): raise AssertionError("never a new token")


def ev():
    out = []
    return out, (lambda step, st, msg: out.append((step, st, msg)))


G = lab.inventory(InvPve(VMS, CTS, NOTES), "pve01", set())
ST = {g["vmid"]: g["status"] for g in G}
LIC = [{"serial": "FMVM1", "status": "ACTIVE"}, {"serial": "FGVMH1", "status": "ACTIVE"}, {"serial": "FGVM11", "status": "EXPIRED"}]
p, f = PowerPve(ST, stubborn={113}), Flex()
log, emit = ev()
r = lab.stop(p, "pve01", G, LIC, f, emit, timeouts={"default": 1, "fmg": 2})
order = [c[1] for c in p.calls if c[0] == "shutdown"]
assert order == [118, 121, 113, 116, 112, 111], order                 # clients, tgen, FortiGates, wan-emu, FortiManager
assert 115 not in order                                                 # already stopped: skipped
assert ("stop", 113) in p.calls and p.calls.index(("stop", 113)) > p.calls.index(("shutdown", 113, 1))   # hard stop after
assert ("shutdown", 111, 2) in p.calls                                  # FortiManager: its own (longer) timeout
assert f.calls == [("stop", "FMVM1"), ("stop", "FGVMH1")], f.calls     # EXPIRED skipped; after all guests
assert all(s == "stopped" for s in p.st.values()) and r["warnings"] >= 1
assert any(st == "ok" for s, st, _ in log if s == "licences")
# no FortiFlex: no licence step
p, log2 = PowerPve(ST), None
log2, emit2 = ev()
lab.stop(p, "pve01", G, [], None, emit2, timeouts={"default": 1, "fmg": 1})
assert any(s == "licences" and st == "skipped" for s, st, _ in log2), log2
# cancel during Stop: the licences stay
evt = threading.Event(); evt.set(); cancel.bind(evt)
f = Flex()
try:
    lab.stop(PowerPve(ST), "pve01", G, LIC, f, ev()[1], timeouts={"default": 1, "fmg": 1})
    raise SystemExit("not cancelled")
except cancel.Cancelled:
    pass
cancel.bind(None)
assert f.calls == [], f.calls
print("OK  lab stop: volgorde, overslaan wat al uit is, netjes en anders hard, licenties pas daarna; zonder FortiFlex/annuleren geen licenties")

ST2 = {k: "stopped" for k in ST}; ST2[112] = "running"                  # wan-emu already on
LIC2 = [{"serial": "FMVM1", "status": "STOPPED"}, {"serial": "FGVMH1", "status": "STOPPED"}, {"serial": "FGVM11", "status": "ACTIVE"}]
p, f = PowerPve(ST2, broken={121}), Flex(fail={"FGVMH1"})
seen = []
checks = [lambda: (seen.append(1), {"site1-H1": "up", "site1-1": "up"})[1]]        # site1-2 never comes back
log, emit = ev()
r = lab.start(p, "pve01", G, LIC2, f, checks, emit, timeouts={"check": 0.05, "poll": 0.01})
assert f.calls == [("reactivate", "FMVM1"), ("reactivate", "FGVMH1")], f.calls      # only STOPPED, nothing created
assert [c[1] for c in p.calls if c[0] == "start"] == [111, 113, 115, 116, 118, 121], p.calls   # FMG, (wan-emu on), FGTs, clients
assert any(s == "licences" and st == "warn" and "FGVMH1" in m for s, st, m in log), log
assert any(s == "start" and st == "warn" and "SDW-trafficgen" in m for s, st, m in log), log     # failed start: goes on
assert any(s == "check" and st == "warn" and "site1-2" in m for s, st, m in log), log
assert r["warnings"] >= 3 and seen
log, emit = ev()
lab.start(PowerPve(ST2), "pve01", G, [], None, [], emit, timeouts={"check": 0.05, "poll": 0.01})
assert any(s == "check" and st == "skipped" for s, st, _ in log) and any(s == "licences" and st == "skipped" for s, st, _ in log)
print("OK  lab start: alleen STOPPED reactiveren (nooit nieuw), volgorde FMG > wan-emu > FGT > clients, controle in FortiManager")

# ---- Task 4: endpoints as jobs, locks shared with deploy and cleanup
import time  # noqa: E402


class LabFake(InvPve, PowerPve):
    def __init__(self):
        InvPve.__init__(self, VMS, CTS, NOTES)
        PowerPve.__init__(self, {g["vmid"]: g["status"] for g in G})


FAKE = LabFake()
main._cleanup_pve = lambda c: FAKE
main._tool_licenses = lambda cl, cfg: ENTS
main._flex_client = lambda *a, **k: Flex()
main._creds = lambda: {}
main.lab.TIMEOUTS.update(default=1, fmg=1, check=0.05, poll=0.01)
pl = main.lab_plan(main.LabReq(server_id=1, node="pve01"))
assert pl["prefix"] == "SDW-" and pl["flex"] == "fortiflex" and len(pl["guests"]) == 7, pl
assert {l["serial"]: l["source"] for l in pl["licences"]}["FGVM12"] == "name"
try:
    main.lab_stop(main.LabReq(server_id=1, node="pve01", serials=["FGVM99"]))           # not a licence of this lab
    raise SystemExit("no 422")
except main.HTTPException as e:
    assert e.status_code == 422, e.status_code
jid = main.lab_stop(main.LabReq(server_id=1, node="pve01", serials=["FMVM1", "FGVMH1"]))["job"]
job = main.JOBS[jid]
assert job["kind"] == "lab-stop" and [s["id"] for s in job["steps_def"]] == ["shutdown", "licences"], job.get("steps_def")
for _ in range(200):
    if job["state"] != "running":
        break
    time.sleep(0.02)
assert job["state"] == "done", (job["state"], job["events"][-3:])
assert all(s == "stopped" for s in FAKE.st.values()) and job["steps"]["licences"] in ("ok", "warn"), job["steps"]
# a running lab job blocks another lab job, a deployment and a cleanup (shared lock)
main.JOBS["busy0001"] = {"id": "busy0001", "state": "running", "dry_run": False, "kind": "lab-start", "events": [], "steps": {}}
for fn in (lambda: main.lab_start(main.LabReq(server_id=1, node="pve01", serials=[])),
           lambda: main.cleanup_run(main.CleanupReq(server_id=1, node="pve01", confirm="DELETE"))):
    try:
        fn()
        raise SystemExit("no 409")
    except main.HTTPException as e:
        assert e.status_code == 409, e.status_code
assert "_busy_refuse(" in inspect.getsource(main.deploy)
del main.JOBS["busy0001"]
jid = main.lab_start(main.LabReq(server_id=1, node="pve01", serials=[]))["job"]
assert [s["id"] for s in main.JOBS[jid]["steps_def"]] == ["licences", "start", "check"]
print("OK  lab: /api/lab/plan, stop/start als taak met eigen stappen, alleen licenties van dit lab, gedeelde vergrendeling")
# a lab job read back from the database (after a restart) still has its own steps
for _ in range(200):
    if main.JOBS[jid]["state"] != "running":
        break
    time.sleep(0.02)
main.JOBS.pop(jid)
back = main._get_job(jid)
assert back and [s["id"] for s in back.get("steps_def", [])] == ["licences", "start", "check"] and back["kind"] == "lab-start", back
print("OK  lab: taak uit de database houdt zijn eigen stappen")
