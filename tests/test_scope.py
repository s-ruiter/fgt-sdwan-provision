"""Deploy scope: everything, only the FortiManager, or only the FortiGates (fake Proxmox + mock FMG).
Run: ORCH_DIR=../sdwan-advpn-reference python tests/test_scope.py"""
import os
import sys
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from app import fmgtemplates, main, model
from app.fmg import Fmg, FmgError, pipeline
from tests import fake_proxmox, mock_fmg
from tests.test_day0_units import fake_qcow2, topo as base_topo

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))
PVE, FMG = "http://127.0.0.1:8095", "http://127.0.0.1:8096"
fake_proxmox.reset()
fake_proxmox.start(8095)
srv = HTTPServer(("127.0.0.1", 8096), mock_fmg.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

t = base_topo()
t["pve"].update(url=PVE, token_id="root@pam!deploy", node="pve1", disk_storage="local-lvm", import_storage="local",
                iso_storage="local", default_bridge="vmbr0")
devs = t["hubs"] + t["edges"]
cfg = fmgtemplates.build_plan(t, ORCH)
td = tempfile.mkdtemp()
fgt_img, fmg_img = Path(td, "fortios.qcow2"), Path(td, "fmg.qcow2")
fgt_img.write_bytes(fake_qcow2())
fmg_img.write_bytes(fake_qcow2())
PW = "Sup3r-Secret-Pw"
TOKS = {d["name"]: f"FGTTOKEN{i}ABCDEFGHIJ" for i, d in enumerate(devs)}


def boot(**kw):
    b = {"dry": False, "fmg": "day0", "fgt": "day0", "fmg_token": "FMGTOKEN0123456789", "fgt_tokens": TOKS,
         "fmg_new_pw": PW, "fgt_new_pw": PW, "pve_secret": "pve-secret", "images": {"fgt": str(fgt_img), "fmg": str(fmg_img)},
         "waiter": lambda h, p, up, tmo: True, "poll": 0, "login_delay": 0, "online_timeout": 5, "fgt_boot_timeout": 5}
    b.update(kw)
    return b


def run(**kw):
    ev = []
    pipeline(t, cfg, Fmg(FMG, "admin", PW), lambda s, st, m: ev.append((s, st, m)), boot(**kw))
    last = {}
    for s, st, _ in ev:
        if st != "info":
            last[s] = st
    return last, ev


names = lambda: {v["name"] for v in fake_proxmox.STATE["vms"].values()}

# ---------------------------------------------------------------- only the FortiManager
last, ev = run(scope="fmg")
assert names() == {"fmg"}, names()
assert {k: last[k] for k in ("pve_fgt", "bootstrap_fgt", "install")} == dict.fromkeys(("pve_fgt", "bootstrap_fgt", "install"), "skipped"), last
assert all(last[k] == "ok" for k in ("pve_fmg", "bootstrap_fmg", "preflight", "adom", "devices", "templates")), last
assert any(s == "pve_fgt" and "alleen FortiManager" in m for s, _, m in ev)
assert not any("/securityconsole/install" in m for _, _, m in ev)
print("OK  alleen FortiManager: alleen de FMG-VM, ADOM/devices/templates aangemaakt, FortiGates en install overgeslagen")

# ---------------------------------------------------------------- only the FortiGates (FMG from the previous run)
last, ev = run(scope="fgt")
assert names() == {"fmg", *TOKS}, names()
assert fake_proxmox.STATE["creates"] == 1 + len(devs), fake_proxmox.STATE["creates"]       # FMG not created again
assert last["pve_fmg"] == last["bootstrap_fmg"] == "skipped", last
assert any(s == "pve_fmg" and "alleen FortiGates" in m for s, _, m in ev)
assert all(last[k] == "ok" for k in ("preflight", "adom", "devices", "pve_fgt", "bootstrap_fgt", "templates", "install")), last
assert any(s == "adom" and "hergebruikt" in m for s, _, m in ev)
print("OK  alleen FortiGates: geen FMG-VM, bestaande ADOM hergebruikt, FortiGate-VM's gemaakt, install uitgevoerd")

# ---------------------------------------------------------------- 'fgt' without a reachable FortiManager: clear error
try:
    pipeline(t, cfg, Fmg("http://127.0.0.1:1", "admin", PW), lambda *a: None, boot(scope="fgt", fgt="none"))
    raise AssertionError("moest falen")
except FmgError as e:
    assert "bestaande" in str(e) or "bereikbare FortiManager" in str(e), e
print("OK  alleen FortiGates zonder bereikbare FortiManager: duidelijke foutmelding")

# ---------------------------------------------------------------- everything (default) is unchanged
fake_proxmox.reset()
last, _ = run()
assert names() == {"fmg", *TOKS} and all(v == "ok" for v in last.values()), (names(), last)
try:
    run(scope="bogus")
    raise AssertionError("moest falen")
except FmgError:
    pass
print("OK  alles (standaard): ongewijzigd; onbekende keuze geweigerd")

# ---------------------------------------------------------------- API: scope overrides the methods + validation
def req(scope, **sec):
    s = {"fgt_tokens": {}, "fgt_new_pw": "", **sec}
    return main.DeployReq(topology={}, fmg=main.Fmgcfg(password=PW), scope=scope, bootstrap={"fmg": "day0", "fgt": "day0"},
                          secrets=s, images={})


r = req("fmg")
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("day0", "none")
r = req("fgt")
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("none", "day0")
try:
    main._apply_scope(req("x"))
    raise AssertionError("moest falen")
except HTTPException as e:
    assert e.status_code == 422


def errors(r):
    try:
        main._check_bootstrap(t, r)
        return []
    except HTTPException as e:
        return e.detail


r = req("fgt", fgt_tokens=TOKS, fgt_new_pw=PW, pve_token_secret="x")
main._apply_scope(r)
e = errors(r)
assert not any("FortiManager: FortiFlex" in x or "FortiManager: image" in x for x in e), e      # FMG token/image not needed
r = req("fmg", fmg_token="FMGTOKEN0123456789", pve_token_secret="x")
main._apply_scope(r)
e = errors(r)
assert not any("FortiFlex-token ontbreekt" in x and "FortiManager" not in x for x in e), e     # FGT tokens not needed
assert not any("FortiGates: admin-wachtwoord" in x or "FortiGate: image" in x for x in e), e
r = req("fgt", fgt_tokens=TOKS, fgt_new_pw=PW, pve_token_secret="x")
r.fmg.password = None
main._apply_scope(r)
assert any("FortiManager: admin-wachtwoord" in x for x in errors(r))                          # API login always needed
print("OK  API: keuze zet de methode van het andere product op 'none'; alleen de benodigde tokens/images/wachtwoorden verplicht")
print("alle scope-tests geslaagd")
