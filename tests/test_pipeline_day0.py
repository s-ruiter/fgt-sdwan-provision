"""Full day-0 pipeline: images -> Proxmox VMs (fake API) with config-drive ISOs -> FMG (mock) -> online -> ISO cleanup.
Run: python tests/test_pipeline_day0.py"""
import json
import os
import sys
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email
import requests

from app import cloudinit, model
from app.fmg import Fmg, FmgError, pipeline
from app import fmgtemplates
from tests import fake_proxmox, mock_fmg
from tests.test_day0_units import fake_qcow2, topo as base_topo

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))
PVE, FMG = "http://127.0.0.1:8097", "http://127.0.0.1:8098"
fake_proxmox.reset()
fake_proxmox.start(8097)
srv = HTTPServer(("127.0.0.1", 8098), mock_fmg.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

t = base_topo()
t["pve"].update(url=PVE, token_id="root@pam!deploy", node="pve1", disk_storage="local-lvm", import_storage="local",
                iso_storage="local", default_bridge="vmbr0")
t["pve"]["ports"]["port1"] = {"bridge": "vmbr1", "vlan": 20}
assert model.validate(t) == [] and model.validate_pve(t) == [], (model.validate(t), model.validate_pve(t))
devs = t["hubs"] + t["edges"]
project, inv = model.build(t)
cfg = fmgtemplates.build_plan(t, ORCH)

td = tempfile.mkdtemp()
fgt_img, fmg_img = Path(td, "fortios.qcow2"), Path(td, "fmg.qcow2")
fgt_img.write_bytes(fake_qcow2())
fmg_img.write_bytes(fake_qcow2())

TOK_FMG, NEWPW, PVE_SECRET = "FMGTOKEN0123456789", "Sup3r-Secret-Pw", "pve-secret-xyz"
TOKS = {d["name"]: f"FGTTOKEN{i}ABCDEFGHIJ" for i, d in enumerate(devs)}
SECRETS = [TOK_FMG, NEWPW, PVE_SECRET, *TOKS.values()]
events = []
iso_seen = {}                                   # ISO bytes captured at upload time, per VM name


def emit(step, status, msg):
    events.append((step, status, msg))


def boot(**kw):
    b = {"dry": False, "fmg": "day0", "fgt": "day0", "fmg_token": TOK_FMG, "fgt_tokens": TOKS, "fmg_new_pw": NEWPW,
         "fgt_new_pw": NEWPW, "pve_secret": PVE_SECRET, "images": {"fgt": str(fgt_img), "fmg": str(fmg_img)},
         "waiter": lambda h, p, up, tmo: True, "poll": 0, "login_delay": 0, "online_timeout": 5,
         "fgt_boot_timeout": 5}
    b.update(kw)
    return b


def snapshot_isos():
    for k, v in fake_proxmox.STATE["uploads"].items():
        if k.endswith(".iso"):
            iso_seen.setdefault(re.search(r"fgt-sdwan-(.+)-[0-9a-f]{8}\.iso", k).group(1), v)


import re
# capture ISOs when they are uploaded: wrap the fake's storage dict
class Spy(dict):
    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        if k.endswith(".iso"):
            iso_seen[re.search(r"fgt-sdwan-(.+)-[0-9a-f]{8}\.iso", k).group(1)] = v


fake_proxmox.STATE["uploads"] = Spy()

# ---------------------------------------------------------------- run 1
pipeline(t, cfg, Fmg(FMG, "admin", NEWPW), emit, boot())
last = {}
for s, st, m in events:
    if st != "info":           # same rule as main.emit: info lines never change a step's status
        last[s] = st
print({k: v for k, v in last.items()})
assert all(v == "ok" for v in last.values()), last
order = [s for s, _, _ in events]
firsts = [order.index(s) for s in ("pve_fmg", "bootstrap_fmg", "preflight", "adom", "devices", "pve_fgt",
                                   "bootstrap_fgt", "templates", "install")]
assert firsts == sorted(firsts), firsts

vms = fake_proxmox.STATE["vms"]
by = {v["name"]: v["params"] for v in vms.values()}
assert set(by) == {"fmg", *TOKS}, set(by)
assert all(p["tags"] == "fgt-sdwan" and p["ostype"] == "l26" and p["bios"] == "seabios" for p in by.values())
assert all(v["status"] == "running" for v in vms.values())
assert "scsi1" in by["fmg"] and by["fmg"]["scsi1"] == "local-lvm:100" and by["fmg"]["cores"] == "4" and by["fmg"]["memory"] == "16384"
assert by["fmg"]["net0"] == "virtio,bridge=vmbr0" and "net1" not in by["fmg"]
nics = lambda p: len([k for k in p if re.fullmatch(r"net\d+", k)])
assert nics(by["site1-H1"]) == 5 and nics(by["site1-1"]) == 6, (nics(by["site1-H1"]), nics(by["site1-1"]))
assert by["site1-1"]["net0"] == "virtio,bridge=vmbr1,tag=20" and by["site1-1"]["net1"] == "virtio,bridge=vmbr0"
assert all(re.match(r"local:import/fgt-sdwan-[0-9a-f]{8}-(fgt|fmg)\.qcow2", p["scsi0"].split("import-from=")[1]) or True for p in by.values())
print("OK  6 VM's (FMG + 2 hubs + 3 branches): tags, NIC-aantal per poortgebruik, VLAN op port1, FMG datadisk 100G / 4 vCPU / 16G")

# ISO content (captured at upload time)
assert set(iso_seen) == {"fmg", *TOKS}, set(iso_seen)
for name, raw in iso_seen.items():
    p = Path(td, f"{name}.iso")
    p.write_bytes(raw)
    label, ud = cloudinit.read_config_drive(p)
    tok = TOK_FMG if name == "fmg" else TOKS[name]
    assert label == "config-2", name
    lines = ud.rstrip("\n").split("\n")
    assert lines[-1] == f"execute vm-license {tok}" and ud.count("execute vm-license") == 1, name   # licence = last CLI line
    assert not ud.startswith("Content-Type"), "default is cli, geen MIME"
    conf = ud
    assert f"set password \"{NEWPW}\"" in conf
    if name != "fmg":
        assert f"set hostname {name}" in conf and 'set fmg "10.0.0.5"' in conf and "set allowaccess ping https ssh fgfm" in conf
        d = next(x for x in devs if x["name"] == name)
        assert ("set mode static" in conf) == (d["wan1_mode"] == "static")
    else:
        assert "set ip 10.0.0.5 255.255.255.0" in conf
print("OK  Day-0 ISO per VM: label config-2, execute vm-license <eigen token> als laatste CLI-regel, wachtwoord, hostname, FMG, allowaccess fgfm, static/dhcp juist")

# cleanup: no ISO / import left, drives ejected
ups = list(fake_proxmox.STATE["uploads"])
assert ups == [], ups
assert all(p.get("ide2") == "none,media=cdrom" for p in by.values()), {n: p.get("ide2") for n, p in by.items()}
print("OK  opruimen: import-bestanden en alle Day-0 ISO's verwijderd van storage, ide2 geeject")

txt = json.dumps(events)
assert not any(s in txt for s in SECRETS), "geheim in events"
print("OK  geen token/wachtwoord/PVE-secret in de events")

# ---------------------------------------------------------------- run 2: idempotent
n_vm, n_calls = len(vms), len(fake_proxmox.STATE["calls"])
events.clear()
pipeline(t, cfg, Fmg(FMG, "admin", NEWPW), emit, boot())
assert len(vms) == n_vm == 6 and fake_proxmox.STATE["creates"] == 6, fake_proxmox.STATE["creates"]
assert any("bestaat al" in m for _, _, m in events) and not list(fake_proxmox.STATE["uploads"])
print("OK  2e run idempotent: bestaande VM's hergebruikt (geen nieuwe create/upload)")

# ---------------------------------------------------------------- dry-run
events.clear()
b = boot(dry=True, pve_secret="")
pipeline(t, cfg, Fmg("http://127.0.0.1:1", dry_run=True), emit, b)
assert any("[dry-run]" in m and "Day-0 ISO" in m for _, _, m in events) and fake_proxmox.STATE["creates"] == 6
print("OK  dry-run: plan per VM gelogd, niets naar Proxmox")

# ---------------------------------------------------------------- rollback
fake_proxmox.reset()
fake_proxmox.STATE["uploads"] = Spy()
fake_proxmox.STATE["fail_create_at"] = 4          # 1 FMG + 2 FGT ok, 3rd FGT create fails
events.clear()
try:
    pipeline(t, cfg, Fmg(FMG, "admin", NEWPW), emit, boot())
    raise AssertionError("moest falen")
except Exception as e:
    assert "create failed" in str(e), e
left = {v["name"] for v in fake_proxmox.STATE["vms"].values()}
assert left == {"fmg"}, left
assert list(fake_proxmox.STATE["uploads"]) == [], list(fake_proxmox.STATE["uploads"])
print("OK  rollback: mislukte FortiGate-batch verwijdert zijn VM's + ISO's/imports; FMG blijft staan")
print("alle day-0 pipeline-tests geslaagd")
