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
from .fmg import Fmg, FmgError, pipeline
from .render import render_inventory

ORCH_DIR = os.environ.get("ORCH_DIR", "/opt/sdwan-advpn-reference")
STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="FGT SD-WAN Provision")
JOBS = {}

STEPS = [
    ("preflight", "Preflight", "Verbinding en FortiManager-versie (8.0.x) controleren"),
    ("adom", "ADOM", "ADOM aanmaken op basis van jouw naam (bestaat hij al: hergebruiken)"),
    ("devices", "Model devices", "2 hubs + 3 branches toevoegen met opgegeven serienummers"),
    ("scripts", "Configuratie", "Per device de gerenderde multi-VRF CLI-config als FMG-script uitvoeren"),
    ("install", "Install", "Device settings installeren zodra de FortiGates online zijn"),
]


class Fmgcfg(BaseModel):
    host: str
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None


class RenderReq(BaseModel):
    topology: dict


class DeployReq(BaseModel):
    topology: dict
    fmg: Fmgcfg
    dry_run: bool = True


def _render(topo):
    errs = model.validate(topo)
    if errs:
        raise HTTPException(422, detail=errs)
    project, inv = model.build(topo)
    try:
        cfg = render_inventory(ORCH_DIR, project, inv)
    except Exception as e:
        raise HTTPException(500, detail=[f"Renderen mislukt: {type(e).__name__}: {e}"])
    return project, inv, cfg


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
    f = Fmg(c.host, c.username, c.password, c.api_token)
    try:
        f.login()
        v = f.call("get", "/sys/status").get("data", {})
        f.logout()
        return {"ok": True, "version": v.get("Version"), "hostname": v.get("Hostname")}
    except FmgError as e:
        raise HTTPException(502, detail=str(e))


def _run(job, topo, cfg, fmgcfg, dry):
    def emit(step, status, msg):
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": step, "status": status, "msg": msg})
        if step in job["steps"]:
            job["steps"][step] = status

    def log(level, msg):
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": None, "status": level, "msg": msg})

    fmg = Fmg(fmgcfg.host, fmgcfg.username, fmgcfg.password, fmgcfg.api_token, dry_run=dry, log=log)
    try:
        pipeline(topo, cfg, fmg, emit)
        job["state"] = "done"
    except Exception as e:
        cur = next((s for s, v in job["steps"].items() if v == "running"), None)
        if cur:
            job["steps"][cur] = "error"
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": cur, "status": "error", "msg": str(e)})
        job["state"] = "failed"
    finally:
        fmg.logout()
        fmgcfg.password = None  # do not keep credentials around


@app.post("/api/deploy")
def deploy(r: DeployReq):
    topo = model.merged(r.topology)
    _, _, cfg = _render(topo)
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "state": "running", "dry_run": r.dry_run, "events": [],
           "steps": {i: "pending" for i, _, _ in STEPS}}
    JOBS[jid] = job
    threading.Thread(target=_run, args=(job, topo, cfg, r.fmg, r.dry_run), daemon=True).start()
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
