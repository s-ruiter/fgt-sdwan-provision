import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, Union

from . import cloudinit, images, model
from .bootstrap import Masker
from .pve import Pve, PveError
from .fmg import Fmg, FmgError, pipeline
from .render import render_inventory

ORCH_DIR = os.environ.get("ORCH_DIR", "/opt/sdwan-advpn-reference")
DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="FGT SD-WAN Provision")
JOBS = {}

STEPS = [
    ("pve_fmg", "FortiManager-VM", "Image naar Proxmox, VM + datadisk + Day-0 config-drive (cloud-init) aanmaken en starten"),
    ("bootstrap_fmg", "FortiManager ZTP", "Vast IP, route, DNS en FortiFlex-licentie: via Day-0 (wachten) of via SSH (execute vm-license)"),
    ("preflight", "Preflight", "Verbinding en FortiManager-versie (8.0.x) controleren"),
    ("adom", "ADOM", "ADOM aanmaken op basis van jouw naam (bestaat hij al: hergebruiken)"),
    ("devices", "Model devices", "2 hubs + 3 branches toevoegen met opgegeven serienummers"),
    ("pve_fgt", "FortiGate-VM's", "Image naar Proxmox, per FortiGate een VM met Day-0 ISO (wan1, DNS, FMG, FortiFlex-token) starten"),
    ("bootstrap_fgt", "FortiGate ZTP", "Day-0: wachten tot ze gelicenseerd online zijn in FMG; of via SSH op bestaande VM's"),
    ("scripts", "Configuratie", "Per device de gerenderde multi-VRF CLI-config als FMG-script uitvoeren"),
    ("install", "Install", "Wachten tot FortiGates online zijn, dan device settings installeren"),
]


class Fmgcfg(BaseModel):
    host: Optional[str] = None      # default: IP from topology.fmgr.ip
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None


class RenderReq(BaseModel):
    topology: dict


class Bootstrap(BaseModel):
    """Per product: 'none', 'ssh' (existing VM, bootstrap over SSH) or 'day0' (tool creates the VM on Proxmox)."""
    fmg: Union[bool, str] = "none"
    fgt: Union[bool, str] = "none"


class Secrets(BaseModel):
    """Never stored, never logged; dropped when the job ends."""
    fmg_token: Optional[str] = None            # FortiFlex token for the FortiManager
    fgt_tokens: dict = {}                      # device name -> FortiFlex token
    fmg_initial_pw: str = ""                   # factory password (blank)
    fgt_initial_pw: str = ""
    fgt_new_pw: str = ""                       # admin password to set on the forced first-login change
    admin_user: str = "admin"
    pve_token_secret: str = ""                 # Proxmox API token secret (day-0 only)


class PveCfg(BaseModel):
    url: str
    token_id: str
    token_secret: str
    verify_tls: bool = True
    node: Optional[str] = None


class PreviewReq(BaseModel):
    topology: dict
    device: str                                # device name, or 'fmg'


class DeployReq(BaseModel):
    topology: dict
    fmg: Fmgcfg
    dry_run: bool = True
    bootstrap: Bootstrap = Bootstrap()
    secrets: Secrets = Secrets()
    images: dict = {}                          # {'fgt': image-id, 'fmg': image-id}


def _render(topo):
    errs = model.validate(topo)
    if errs:
        raise HTTPException(422, detail=errs)
    project, inv = model.build(topo)
    try:
        cfg = render_inventory(ORCH_DIR, project, inv)
    except Exception as e:
        raise HTTPException(500, detail=[f"Renderen mislukt: {type(e).__name__}: {e}"])
    for d in topo["hubs"] + topo["edges"]:
        cfg[d["name"]] = model.append_fragment(cfg[d["name"]], model.underlay_fragment(topo, d))
    return project, inv, cfg


def _method(v):
    return "ssh" if v is True else (v or "none")


def _check_bootstrap(topo, r):
    """Extra validation when ZTP bootstrap / day-0 deployment is requested."""
    errs = []
    b, s = r.bootstrap, r.secrets
    mf, mg = _method(b.fmg), _method(b.fgt)
    for m in (mf, mg):
        if m not in ("none", "ssh", "day0"):
            errs.append(f"onbekende bootstrap-methode '{m}'")
    devs = topo["hubs"] + topo["edges"]
    if "day0" in (mf, mg):
        errs += model.validate_pve(topo)
        if not s.pve_token_secret:
            errs.append("Proxmox: token-secret ontbreekt")
        for role, m in (("fmg", mf), ("fgt", mg)):
            if m == "day0":
                try:
                    meta = images.get_image(DATA_DIR, r.images.get(role, ""))
                    if meta["role"] != role:
                        errs.append(f"image voor {role} is als {meta['role']} geupload")
                except images.ImageError:
                    errs.append(f"{'FortiManager' if role == 'fmg' else 'FortiGate'}: image ontbreekt, upload eerst een image")
    if mf in ("ssh", "day0"):
        if not model.valid_token(s.fmg_token):
            errs.append("FortiManager: FortiFlex-token ontbreekt of is ongeldig (alleen letters/cijfers)")
        if not r.fmg.password:
            errs.append("FortiManager: admin-wachtwoord is verplicht")
    if mg in ("ssh", "day0"):
        for d in devs:
            if not model.valid_token(s.fgt_tokens.get(d["name"])):
                errs.append(f"{d['name']}: FortiFlex-token ontbreekt of is ongeldig")
        if not s.fgt_new_pw:
            errs.append("FortiGates: admin-wachtwoord is verplicht")
    if mg == "ssh":
        ips = [d.get("initial_ip") or model.DEFAULT_INITIAL_IP for d in devs]
        dup = sorted({i for i in ips if ips.count(i) > 1})
        if dup:
            errs.append("Initieel IP moet uniek zijn per FortiGate (nu dubbel: " + ", ".join(dup) +
                        "). Gebruik het DHCP-adres of een eigen VLAN per device tijdens ZTP.")
    if errs:
        raise HTTPException(422, detail=errs)


# --- images (uploaded KVM images) -------------------------------------------------------------------------------
@app.post("/api/images")
def upload_image(role: str = Form(...), file: UploadFile = File(...)):
    try:
        return images.save_upload(DATA_DIR, file.file, file.filename, role)
    except images.ImageError as e:
        raise HTTPException(422, detail=str(e))


@app.get("/api/images")
def list_images():
    return images.list_images(DATA_DIR)


@app.delete("/api/images/{iid}")
def delete_image(iid: str):
    try:
        images.delete_image(DATA_DIR, iid)
    except images.ImageError as e:
        raise HTTPException(404, detail=str(e))
    return {"ok": True}


# --- Proxmox ----------------------------------------------------------------------------------------------------
@app.post("/api/pve/test")
def pve_test(c: PveCfg):
    p = Pve(c.url, c.token_id, c.token_secret, c.verify_tls, timeout=15)
    try:
        res = p.check()
        if c.node:
            res["bridges"] = p.bridges(c.node)
        return res
    except PveError as e:
        raise HTTPException(502, detail=e.detail)


@app.post("/api/cloudinit/preview")
def cloudinit_preview(r: PreviewReq):
    topo = model.merged(r.topology)
    errs = model.validate(topo)
    if errs:
        raise HTTPException(422, detail=errs)
    pw, tok = "<ADMIN-WACHTWOORD>", "XXXXXXXXXXXXXXXXXXXX"
    mode = topo["pve"].get("license_mode", "cli")
    if r.device == "fmg":
        cli = cloudinit.fmg_cli(topo, pw)
    else:
        dev = next((d for d in topo["hubs"] + topo["edges"] if d["name"] == r.device), None)
        if dev is None:
            raise HTTPException(404, detail="onbekend device")
        cli = cloudinit.fgt_cli(dev, topo, pw)
    return {"mode": mode, "layout": "config-2 ISO: /openstack/latest/user_data",
            "user_data": cloudinit.mask(cloudinit.build_user_data(cli, tok, mode), tok)}


@app.get("/api/about")
def about():
    return {"steps": [{"id": i, "title": t, "desc": d} for i, t, d in STEPS],
            "orchestrator": ORCH_DIR, "orchestrator_present": Path(ORCH_DIR).exists()}


@app.get("/api/defaults")
def defaults():
    return model.DEFAULTS


@app.post("/api/validate")
def validate(r: RenderReq):
    return {"errors": model.validate(model.merged(r.topology))}


@app.post("/api/render")
def render(r: RenderReq):
    topo = model.merged(r.topology)
    project, inv, cfg = _render(topo)
    return {"project": project, "inventory": inv, "configs": cfg,
            "lines": {k: len(v.splitlines()) for k, v in cfg.items()}}


@app.post("/api/fmg/test")
def fmg_test(c: Fmgcfg):
    if not c.host:
        raise HTTPException(422, detail="host ontbreekt")
    f = Fmg(c.host, c.username, c.password, c.api_token)
    try:
        f.login()
        v = f.call("get", "/sys/status").get("data", {})
        f.logout()
        return {"ok": True, "version": v.get("Version"), "hostname": v.get("Hostname")}
    except FmgError as e:
        raise HTTPException(502, detail=str(e))


def _run(job, topo, cfg, fmgcfg, dry, boot, mask):
    def emit(step, status, msg):
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": step, "status": status, "msg": mask(msg)})
        if step in job["steps"] and status != "info":
            job["steps"][step] = status

    def log(level, msg):
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": None, "status": level, "msg": mask(msg)})

    host = fmgcfg.host or model.split_cidr(topo["fmgr"]["ip"])[0]
    fmg = Fmg(host, fmgcfg.username, fmgcfg.password, fmgcfg.api_token, dry_run=dry, log=log)
    try:
        pipeline(topo, cfg, fmg, emit, boot)
        job["state"] = "done"
    except Exception as e:
        cur = next((s for s, v in job["steps"].items() if v == "running"), None)
        if cur:
            job["steps"][cur] = "error"
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": cur, "status": "error", "msg": mask(str(e))})
        job["state"] = "failed"
    finally:
        fmg.logout()
        fmgcfg.password = None  # do not keep credentials around
        boot.clear()


@app.post("/api/deploy")
def deploy(r: DeployReq):
    topo = model.merged(r.topology)
    _, _, cfg = _render(topo)
    _check_bootstrap(topo, r)
    s = r.secrets
    mask = Masker(s.fmg_token, s.fmg_initial_pw, s.fgt_initial_pw, s.fgt_new_pw, r.fmg.password, r.fmg.api_token,
                  s.pve_token_secret, *s.fgt_tokens.values())
    img = {}
    for role in ("fmg", "fgt"):
        if r.images.get(role):
            img[role] = images.get_image(DATA_DIR, r.images[role])["path"]
    boot = {"dry": r.dry_run, "fmg": _method(r.bootstrap.fmg), "fgt": _method(r.bootstrap.fgt),
            "images": img, "pve_secret": s.pve_token_secret,
            "fmg_token": s.fmg_token, "fgt_tokens": dict(s.fgt_tokens),
            "fmg_user": s.admin_user, "fgt_user": s.admin_user,
            "fmg_initial_pw": s.fmg_initial_pw, "fmg_new_pw": r.fmg.password or "",
            "fgt_initial_pw": s.fgt_initial_pw, "fgt_new_pw": s.fgt_new_pw}
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "state": "running", "dry_run": r.dry_run, "events": [],
           "steps": {i: "pending" for i, _, _ in STEPS}}
    JOBS[jid] = job
    threading.Thread(target=_run, args=(job, topo, cfg, r.fmg, r.dry_run, boot, mask), daemon=True).start()
    return {"job": jid}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]


@app.get("/api/jobs/{jid}/stream")
def stream(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)

    def gen():
        sent = 0
        while True:
            j = JOBS[jid]
            while sent < len(j["events"]):
                yield f"event: log\ndata: {json.dumps(j['events'][sent])}\n\n"
                sent += 1
            yield f"event: state\ndata: {json.dumps({'state': j['state'], 'steps': j['steps']})}\n\n"
            if j["state"] != "running" and sent >= len(j["events"]):
                break
            time.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Static UI files revalidate on every load, so an update of the tool shows up without a hard refresh."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
