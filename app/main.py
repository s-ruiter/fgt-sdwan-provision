import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from . import model
from .bootstrap import Masker
from .fmg import Fmg, FmgError, pipeline
from .render import render_inventory

ORCH_DIR = os.environ.get("ORCH_DIR", "/opt/sdwan-advpn-reference")
STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="FGT SD-WAN Provision")
JOBS = {}

STEPS = [
    ("bootstrap_fmg", "FortiManager ZTP", "Vast IP, route, DNS en FortiFlex-licentie (execute vm-license) op de FortiManager"),
    ("preflight", "Preflight", "Verbinding en FortiManager-versie (8.0.x) controleren"),
    ("adom", "ADOM", "ADOM aanmaken op basis van jouw naam (bestaat hij al: hergebruiken)"),
    ("devices", "Model devices", "2 hubs + 3 branches toevoegen met opgegeven serienummers"),
    ("bootstrap_fgt", "FortiGate ZTP", "wan1 (port1) statisch of DHCP, DNS, central-management en FortiFlex-licentie per FortiGate"),
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
    fmg: bool = False
    fgt: bool = False


class Secrets(BaseModel):
    """Never stored, never logged; dropped when the job ends."""
    fmg_token: Optional[str] = None            # FortiFlex token for the FortiManager
    fgt_tokens: dict = {}                      # device name -> FortiFlex token
    fmg_initial_pw: str = ""                   # factory password (blank)
    fgt_initial_pw: str = ""
    fgt_new_pw: str = ""                       # admin password to set on the forced first-login change
    admin_user: str = "admin"


class DeployReq(BaseModel):
    topology: dict
    fmg: Fmgcfg
    dry_run: bool = True
    bootstrap: Bootstrap = Bootstrap()
    secrets: Secrets = Secrets()


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


def _check_bootstrap(topo, r):
    """Extra validation when ZTP bootstrap is requested."""
    errs = []
    b, s = r.bootstrap, r.secrets
    devs = topo["hubs"] + topo["edges"]
    if b.fmg:
        if not model.valid_token(s.fmg_token):
            errs.append("FortiManager: FortiFlex-token ontbreekt of is ongeldig (alleen letters/cijfers)")
        if not r.fmg.password:
            errs.append("FortiManager: (nieuw) admin-wachtwoord is verplicht voor de eerste login")
    if b.fgt:
        for d in devs:
            if not model.valid_token(s.fgt_tokens.get(d["name"])):
                errs.append(f"{d['name']}: FortiFlex-token ontbreekt of is ongeldig")
        if not s.fgt_new_pw:
            errs.append("FortiGates: nieuw admin-wachtwoord is verplicht voor de eerste login")
        ips = [d.get("initial_ip") or model.DEFAULT_INITIAL_IP for d in devs]
        dup = sorted({i for i in ips if ips.count(i) > 1})
        if dup:
            errs.append("Initieel IP moet uniek zijn per FortiGate (nu dubbel: " + ", ".join(dup) +
                        "). Gebruik het DHCP-adres of een eigen VLAN per device tijdens ZTP.")
    if errs:
        raise HTTPException(422, detail=errs)


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
        if step in job["steps"]:
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
                  *s.fgt_tokens.values())
    boot = {"dry": r.dry_run, "fmg": r.bootstrap.fmg, "fgt": r.bootstrap.fgt,
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


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
