import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, Union

from . import cloudinit, db, images, model
from .bootstrap import Masker
from .pve import Pve, PveError
from .fmg import Fmg, FmgError, pipeline
from . import fmgtemplates

from .config import DATA_DIR, ORCH_DIR
from .routes_servers import router as servers_router
from .routes_settings import router as settings_router
STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="FGT SD-WAN Provision")
app.include_router(servers_router)
app.include_router(settings_router)
JOBS = {}
_JOB_LOCK = threading.Lock()
_JOB_DB = None


def _jobdb():
    global _JOB_DB
    if _JOB_DB is None:
        _JOB_DB = db.connect(DATA_DIR)
        db.interrupt_running_jobs(_JOB_DB, time.strftime("%H:%M:%S"))      # jobs killed by a restart
    return _JOB_DB


def _persist(job):
    try:
        with _JOB_LOCK:
            db.save_job(_jobdb(), job)
    except Exception:                                    # progress storage must never break a deployment
        pass


def _get_job(jid):
    j = JOBS.get(jid)
    if j is not None:
        return j
    with _JOB_LOCK:
        return db.load_job(_jobdb(), jid)

STEPS = [
    ("licenses", "Licences", "FortiFlex: per device reuse an existing licence (new token) or create a new one; serial number and token go into the deployment"),
    ("pve_fmg", "FortiManager VM", "Image to Proxmox, create and start the VM + data disk + Day-0 config drive (cloud-init)"),
    ("bootstrap_fmg", "FortiManager ZTP", "Static IP, route, DNS and FortiFlex licence: via Day-0 (wait) or via SSH (execute vm-license)"),
    ("preflight", "Preflight", "Check the connection and the FortiManager version (8.0.x)"),
    ("fmg_settings", "FortiManager settings", "Idle timeouts 180 min, time zone Europe/Amsterdam, ADOMs in advanced mode, FortiAnalyzer features on (reboot)"),
    ("adom", "ADOM", "Create the ADOM with your name (reused when it exists)"),
    ("devices", "Model devices", "Add the hubs and branches with their serial numbers"),
    ("templates", "Templates", "Metadata variables, system, SD-WAN and CLI templates, policy packages, template groups and blueprints in the ADOM"),
    ("pve_wan", "WAN emulator", "LXC wan-emu: routes ISP-HUB / ISP-A / ISP-B (NAT to the lab), DHCP for the spokes, traffic shaping per link, port forwards"),
    ("pve_fgt", "FortiGate VMs", "Image to Proxmox, per FortiGate a VM with Day-0 ISO (wan1: hub static IP + gateway, branch DHCP; DNS, FMG link, FortiFlex licence), started"),
    ("bootstrap_fgt", "FortiGate ZTP", "Day-0: wait until they are licensed and online in FMG; or via SSH on existing VMs"),
    ("install", "Install", "Quick install (device settings) per device, then Re-install policy per role; real FortiGates once they are online"),
    ("clients", "LAN clients", "Per spoke a container on the LAN1 VLAN (.10, spoke as gateway) to test traffic; operated from the Proxmox console. Optional: Traffic-Gen in the LAN of the first spoke"),
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
    client_pw: str = ""                        # root password of the LAN client containers
    tg_pw: str = ""                            # admin password of the Traffic-Gen web GUI (optional container)
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
    scope: str = "all"                         # 'all', 'fmg' (only FortiManager), 'fgt' (only FortiGates), 'tpl' (only templates), 'dev' (only model devices)


SCOPE_TITLE = {"all": "", "fmg": " - FortiManager only", "fgt": " - FortiGates only", "tpl": " - templates only", "dev": " - model devices only",
               "lan": " - LAN clients only"}


def _apply_scope(r):
    """The scope wins over the per-product method: a product that is not deployed gets method 'none'."""
    if r.scope not in SCOPE_TITLE:
        raise HTTPException(422, detail=[f"unknown deployment choice '{r.scope}' (all, fmg, fgt, tpl, dev or lan)"])
    if r.scope == "fgt":
        r.bootstrap.fmg = "none"
    elif r.scope == "fmg":
        r.bootstrap.fgt = "none"
    elif r.scope in ("tpl", "dev", "lan"):     # existing FortiManager / only the LAN clients: no FMG or FGT VMs
        r.bootstrap.fmg = r.bootstrap.fgt = "none"


def _render(topo, create_psk=False, devices=True, fmg_vm=True):
    errs = model.validate(topo, devices, fmg_vm)
    if errs:
        raise HTTPException(422, detail=errs)
    c = db.connect(DATA_DIR)
    try:
        topo["psk"] = db.get_psk(c, topo["adom"], create=create_psk)      # always generated by the tool, never typed
    finally:
        c.close()
    project, inv = model.build(topo)
    try:
        plan = fmgtemplates.build_plan(topo, ORCH_DIR)
    except Exception as e:
        raise HTTPException(500, detail=[f"Generating the templates failed: {type(e).__name__}: {e}"])
    return project, inv, plan


def _method(v):
    return "ssh" if v is True else (v or "none")


def _check_bridges(topo, r):
    """The bridges this run uses exist on the node and carry its VLANs (asks Proxmox; nothing is created)."""
    from . import provision_vms as pv
    from .pve import PveError
    from .wanemu import net
    fgts = _method(r.bootstrap.fgt) == "day0" and r.scope not in ("fmg", "tpl", "dev", "lan")
    if not (fgts or r.scope == "lan"):
        return
    try:
        info = pv.make_pve(topo["pve"], r.secrets.pve_token_secret).bridge_info(topo["pve"]["node"])
    except PveError as e:
        raise HTTPException(422, detail=[f"Proxmox: cannot read the bridges of node {topo['pve']['node']}: {e}"])
    problems = net.bridge_problems(net.bridge_needs(topo, fgts, True), info)
    if fgts or (r.scope == "lan" and (topo.get("trafficgen") or {}).get("enabled")):
        problems += net.uplink_problems(topo, info)
    if problems:
        raise HTTPException(422, detail=[f"Proxmox: {x}" for x in problems] +
                            ["Choose the bridges under WAN emulation: the uplink bridge to your lab, the WAN emulator "
                             "bridge for the ISP segments and the LAN VLANs"])


def _check_trafficgen(topo, s):
    from . import trafficgen
    if not trafficgen.enabled(topo):
        return []
    errs = trafficgen.validate(topo)
    if model.password_problems(s.tg_pw):
        errs.append("Traffic-Gen: GUI password is not strong, it needs " + ", ".join(model.password_problems(s.tg_pw)))
    return errs


def _check_bootstrap(topo, r):
    """Extra validation when ZTP bootstrap / day-0 deployment is requested."""
    errs = []
    b, s = r.bootstrap, r.secrets
    mf, mg = _method(b.fmg), _method(b.fgt)
    if r.scope == "lan":                       # LAN clients only: Proxmox + the client root password
        if not topo["pve"].get("server_id") and not topo["pve"].get("url"):
            errs.append("Proxmox: choose a server (Servers page) or add one")
        else:
            errs += model.validate_pve(topo)
            if not s.pve_token_secret:
                errs.append("Proxmox: token secret is missing")
        if len(s.client_pw or "") < 5:
            errs.append("LAN client password: at least 5 characters (root password of the client containers)")
        errs += _check_trafficgen(topo, s)
        if errs:
            raise HTTPException(422, detail=errs)
        return
    for m in (mf, mg):
        if m not in ("none", "ssh", "day0"):
            errs.append(f"unknown bootstrap method '{m}'")
    devs = topo["hubs"] + topo["edges"]
    if "day0" in (mf, mg):
        if not topo["pve"].get("server_id") and not topo["pve"].get("url"):
            errs.append("Proxmox: choose a server (Servers page) or add one")
        else:
            errs += model.validate_pve(topo, wanemu=mg == "day0" and r.scope not in ("fmg", "tpl", "dev"))
            if not s.pve_token_secret:
                errs.append("Proxmox: token secret is missing")
        for role, m in (("fmg", mf), ("fgt", mg)):
            if m == "day0":
                try:
                    meta = images.get_image(DATA_DIR, r.images.get(role, ""))
                    if meta["role"] != role:
                        errs.append(f"image for {role} was uploaded as {meta['role']}")
                except images.ImageError:
                    errs.append(f"{'FortiManager' if role == 'fmg' else 'FortiGate'}: image missing, upload an image first")
    if mf in ("ssh", "day0"):
        if not model.valid_token(s.fmg_token):
            errs.append("FortiManager: FortiFlex token missing or invalid (letters/digits only)")
    if not r.fmg.password and not r.fmg.api_token:     # the tool always logs in to the FortiManager API
        errs.append("FortiManager: admin password is required")
    elif mf == "day0" and model.password_problems(r.fmg.password):       # the tool sets it on the new FortiManager
        errs.append("FortiManager: admin password is not strong, it needs " + ", ".join(model.password_problems(r.fmg.password)))
    if mg in ("ssh", "day0"):
        for d in devs:
            if not model.valid_token(s.fgt_tokens.get(d["name"])):
                errs.append(f"{d['name']}: FortiFlex token missing or invalid")
        if not s.fgt_new_pw:
            errs.append("FortiGates: admin password is required")
        elif model.password_problems(s.fgt_new_pw):
            errs.append("FortiGates: admin password is not strong, it needs " + ", ".join(model.password_problems(s.fgt_new_pw)))
    if mg == "day0" and r.scope in ("all", "fgt") and len(s.client_pw or "") < 5:   # the LAN clients come along
        errs.append("LAN client password: at least 5 characters (root password of the client containers)")
    if mg == "day0" and r.scope in ("all", "fgt"):
        errs += _check_trafficgen(topo, s)
    if mg == "ssh":
        ips = [d.get("initial_ip") or model.DEFAULT_INITIAL_IP for d in devs]
        dup = sorted({i for i in ips if ips.count(i) > 1})
        if dup:
            errs.append("The initial IP must be unique per FortiGate (duplicate now: " + ", ".join(dup) +
                        "). Use the DHCP address or a separate VLAN per device during ZTP.")
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
    pw, tok = "<ADMIN-PASSWORD>", "XXXXXXXXXXXXXXXXXXXX"
    mode = cloudinit.license_mode("fmg" if r.device == "fmg" else "fgt", topo["pve"].get("license_mode", "cli"))
    if r.device == "fmg":
        cli = cloudinit.fmg_cli(topo, pw)
    else:
        dev = next((d for d in topo["hubs"] + topo["edges"] if d["name"] == r.device), None)
        if dev is None:
            raise HTTPException(404, detail="unknown device")
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
    project, inv, plan = _render(topo)
    cfg = fmgtemplates.preview(plan)
    return {"project": project, "inventory": inv, "configs": cfg, "warnings": plan["warnings"],
            "roles": [{"name": r["name"], "kind": r["kind"], "devices": r["devices"]} for r in plan["roles"]],
            "lines": {k: len(v.splitlines()) for k, v in cfg.items()}}


@app.post("/api/fmg/test")
def fmg_test(c: Fmgcfg):
    if not c.host:
        raise HTTPException(422, detail="host is missing")
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
        _persist(job)

    def log(level, msg):
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": None, "status": level, "msg": mask(msg)})

    host = fmgcfg.host or (model.split_cidr(topo["fmgr"]["ip"])[0] if topo["fmgr"].get("ip") else "127.0.0.1")   # lan: unused
    fmg = Fmg(host, fmgcfg.username, fmgcfg.password, fmgcfg.api_token, dry_run=dry, log=log)
    try:
        pipeline(topo, cfg, fmg, emit, boot)
        job["state"] = "done"
        _persist(job)
    except Exception as e:
        cur = next((s for s, v in job["steps"].items() if v == "running"), None)
        if cur:
            job["steps"][cur] = "error"
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": cur, "status": "error", "msg": mask(str(e))})
        job["state"] = "failed"
        _persist(job)
    finally:
        fmg.logout()
        fmgcfg.password = None  # do not keep credentials around
        boot.clear()


def _wanemu_secrets():
    """SSH key (root login in wan-emu) and agent token: generated once, kept in the tool database (0600)."""
    from .wanemu import setup
    conn = db.connect(DATA_DIR)
    return {"ssh_key": db.get_secret(conn, "wanemu_ssh_key", create=setup.new_ssh_key),
            "agent_token": db.get_secret(conn, "wanemu_agent_token", create=lambda: __import__("secrets").token_urlsafe(32))}


# ---- WAN-emulatie: proxy to the agent in wan-emu (the token stays in the tool) ------------------------------------
def _agent():
    from .wanemu.client import Agent
    conn = db.connect(DATA_DIR)
    addr, tok = db.get_secret(conn, "wanemu_addr"), db.get_secret(conn, "wanemu_agent_token")
    if not addr or not tok:
        raise HTTPException(409, detail="No WAN emulator deployed yet: deploy FortiGates first (Everything or FortiGates only)")
    return Agent(addr, tok)


def _agent_call(fn):
    from .wanemu.client import AgentError
    try:
        return fn()
    except AgentError as e:
        raise HTTPException(e.status, detail=str(e))


# ---- Traffic tab: the deployed Traffic-Gen, through its web API (only the traffic test) ---------------------------
TRAFFIC_SCHEME = "https"                       # Traffic-Gen serves HTTPS with its own certificate (tests: http)
TRAFFIC_START = ("duration_minutes", "threads_per_ip", "intensity", "groups")
TRAFFIC_CONFIG = ("duration_minutes", "threads_per_ip", "intensity", "groups", "disabled_apps", "tls_verify")


def _traffic():
    from .traffic_client import TrafficClient
    conn = db.connect(DATA_DIR)
    addr, pw = db.get_secret(conn, "trafficgen_addr"), db.get_secret(conn, "trafficgen_password")
    if not addr or not pw:
        raise HTTPException(409, detail="No Traffic-Gen deployed yet: tick 'Also deploy Traffic-Gen' (LAN clients) and deploy")
    return TrafficClient(addr, pw, scheme=TRAFFIC_SCHEME)


def _traffic_call(fn):
    from .traffic_client import TrafficError
    try:
        return fn()
    except TrafficError as e:
        raise HTTPException(e.status, detail=str(e))


@app.get("/api/traffic/status")
def traffic_status():
    conn = db.connect(DATA_DIR)
    if not (db.get_secret(conn, "trafficgen_addr") and db.get_secret(conn, "trafficgen_password")):
        return {"deployed": False}
    from .traffic_client import TrafficError
    cl = _traffic()
    out = {"deployed": True, "url": cl.base, "reachable": True, "error": ""}
    try:
        out["state"] = cl.state()
    except TrafficError as e:
        out.update(reachable=False, error=str(e))
    return out


@app.get("/api/traffic/state")
def traffic_state():
    cl = _traffic()
    return _traffic_call(cl.state)


@app.get("/api/traffic/config")
def traffic_config():
    cl = _traffic()
    return _traffic_call(cl.config)


@app.post("/api/traffic/config")
def traffic_set_config(body: dict = Body(...)):
    cl = _traffic()
    return _traffic_call(lambda: cl.set_config({k: v for k, v in body.items() if k in TRAFFIC_CONFIG}))


@app.get("/api/traffic/apps")
def traffic_apps():
    cl = _traffic()
    return _traffic_call(cl.apps)


@app.get("/api/traffic/log")
def traffic_log(n: int = 60):
    cl = _traffic()
    return _traffic_call(lambda: cl.log(max(1, min(n, 300))))


@app.post("/api/traffic/start")
def traffic_start(body: dict = Body(...)):
    cl = _traffic()
    return _traffic_call(lambda: cl.start({k: v for k, v in body.items() if k in TRAFFIC_START}))


@app.post("/api/traffic/stop")
def traffic_stop():
    cl = _traffic()
    return _traffic_call(cl.stop)


@app.get("/api/wanemu/links")
def wanemu_links():
    from .wanemu.client import PRESETS
    a = _agent()
    return {"addr": a.addr, "links": _agent_call(a.links),
            "presets": {k: v["label"] for k, v in PRESETS.items()}}


@app.put("/api/wanemu/links/{link_id}")
def wanemu_put(link_id: str, values: dict = Body(...)):
    a = _agent()
    return _agent_call(lambda: a.put(link_id, values))


@app.post("/api/wanemu/preset/{name}")
def wanemu_preset(name: str, link: Optional[str] = None):
    from .wanemu.client import PRESETS, preset_changes
    if name not in PRESETS:
        raise HTTPException(404, detail=f"unknown preset {name}")
    a = _agent()
    changes = preset_changes(name, _agent_call(a.links), link)
    for lid, vals in changes:
        _agent_call(lambda lid=lid, vals=vals: a.put(lid, vals))
    return {"applied": [lid for lid, _ in changes]}


@app.get("/api/wanemu/forwards")
def wanemu_forwards():
    raw = db.get_secret(db.connect(DATA_DIR), "wanemu_forwards")
    if not raw:
        raise HTTPException(409, detail="No WAN emulator deployed yet: deploy FortiGates first (Everything or FortiGates only)")
    return json.loads(raw)


# ---- remote access: FortiManager (direct) + FortiGates (wan-emu port forwards); admin credentials kept at deployment
def _creds():
    raw = db.get_secret(db.connect(DATA_DIR), "access_creds")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _forwards():
    raw = db.get_secret(db.connect(DATA_DIR), "wanemu_forwards")
    return json.loads(raw) if raw else {"host": "", "forwards": []}


@app.get("/api/access")
def access():
    """What the Access page lists: GUI URL + SSH command per device; whether the tool knows the password."""
    cr, fw = _creds(), _forwards()
    fm = cr.get("fmg") or {}
    fmg = ({"name": fm.get("name") or "FortiManager", "ip": fm["host"], "gui": f"https://{fm['host']}/",
            "ssh": f"ssh {fm.get('user') or 'admin'}@{fm['host']}", "password": bool(fm.get("password"))}
           if fm.get("host") else None)
    fg = cr.get("fgt") or {}
    fgts = [{"name": f["name"], "ip": f["ip"], "gui": f"https://{fw['host']}:{f['https']}/",
             "ssh": f"ssh -p {f['ssh']} {fg.get('user') or 'admin'}@{fw['host']}", "password": bool(fg.get("password"))}
            for f in fw["forwards"]]
    return {"fmg": fmg, "fgts": fgts}


class AccessName(BaseModel):
    name: str


@app.post("/api/access/password")
def access_password(a: AccessName):
    """The kept admin login of one device, for the copy-password button (only on request)."""
    cr, fw = _creds(), _forwards()
    fm, fg = cr.get("fmg") or {}, cr.get("fgt") or {}
    if a.name in ("fmg", "@fmg", fm.get("name")) and fm.get("password"):
        return {"user": fm.get("user") or "admin", "password": fm["password"]}
    if any(f["name"] == a.name for f in fw["forwards"]) and fg.get("password"):
        return {"user": fg.get("user") or "admin", "password": fg["password"]}
    raise HTTPException(404, detail=f"no password kept for {a.name}")


# ---- SSH in the browser: websocket <-> SSH shell (FortiGates via the wan-emu port forward, FortiManager directly).
# Without a typed password the admin login kept at deployment is used (server side: it never reaches the browser).
class TermError(Exception):
    pass


def _term_connect(host, port, user, password, cols, rows):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())          # lab FortiGates, new host key on every deploy
    try:
        c.connect(host, port=port, username=user, password=password, timeout=10, banner_timeout=15,
                  auth_timeout=15, allow_agent=False, look_for_keys=False)
    except paramiko.AuthenticationException:
        c.close()
        raise TermError("login refused (wrong user or password)") from None
    except Exception as e:
        c.close()
        raise TermError(f"no SSH connection to {host}:{port} ({type(e).__name__})") from None
    try:
        chan = c.invoke_shell(term="xterm", width=cols, height=rows)
    except Exception as e:
        c.close()
        raise TermError(f"no shell on {host}:{port} ({type(e).__name__})") from None
    return chan, c.close


TERM_CONNECT = _term_connect


@app.websocket("/api/term/{name}")
async def term(ws: WebSocket, name: str):
    await ws.accept()

    async def say(m):
        try:
            await ws.send_text(f"\r\n\x1b[31m[{m}]\x1b[0m\r\n")
        except Exception:                                            # browser already gone
            pass

    async def close():
        try:
            await ws.close()
        except Exception:
            pass

    chan = closer = None
    try:
        try:
            auth = json.loads(await ws.receive_text())
            cols, rows = int(auth.get("cols") or 80), int(auth.get("rows") or 24)
        except (ValueError, TypeError, AttributeError):
            await say("invalid start message")
            return await close()
        cr = _creds()
        if name == "@fmg":
            fm = cr.get("fmg") or {}
            if not fm.get("host"):
                await say("unknown device FortiManager: deploy first")
                return await close()
            host, port, kept = fm["host"], 22, fm
        else:
            fw = _forwards()
            f = next((x for x in fw["forwards"] if x["name"] == name), None)
            if not f:
                await say(f"unknown device {name}: deploy the FortiGates first")
                return await close()
            host, port, kept = fw["host"], f["ssh"], cr.get("fgt") or {}
        user, pw = auth.get("user") or "", auth.get("password") or ""
        if not pw and kept.get("password"):                          # automatic login with the kept credentials
            user, pw = kept.get("user") or "admin", kept["password"]
        try:
            chan, closer = await asyncio.to_thread(TERM_CONNECT, host, port, user or "admin", pw, cols, rows)
        except TermError as e:
            await say(str(e))
            return await close()
        auth = None                                                   # the password is not kept
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")   # UTF-8 split over chunks

        async def out():
            while not chan.exit_status_ready():
                if chan.recv_ready():
                    await ws.send_text(dec.decode(chan.recv(65536)))
                else:
                    await asyncio.sleep(0.02)
            while chan.recv_ready():
                await ws.send_text(dec.decode(chan.recv(65536)))

        async def inp():
            while True:
                m = json.loads(await ws.receive_text())
                if "data" in m:                                       # sendall in a thread: no partial writes, and
                    await asyncio.to_thread(chan.sendall, str(m["data"]).encode())   # a full window never blocks the loop
                elif "resize" in m:
                    chan.resize_pty(width=int(m["resize"][0]), height=int(m["resize"][1]))

        t_out, t_in = asyncio.create_task(out()), asyncio.create_task(inp())
        done, _ = await asyncio.wait([t_out, t_in], return_when=asyncio.FIRST_COMPLETED)
        for x in (t_out, t_in):
            x.cancel()
        await asyncio.gather(t_out, t_in, return_exceptions=True)    # retrieve, never "exception was never retrieved"
        if t_out in done:                                             # the FortiGate ended the session: tell the browser
            await say("session ended")
            await close()
    except Exception:                                                 # disconnects and anything else: just clean up
        pass
    finally:
        if chan is not None:
            chan.close()
        if closer is not None:
            closer()


@app.post("/api/wanemu/reset")
def wanemu_reset():
    a = _agent()
    return {"links": _agent_call(a.reset)}


# ---- FortiFlex: credentials + chosen configurations (tool database, 0600); licenses per device at deployment ------
class FlexCreds(BaseModel):
    user: str = ""
    password: str = ""                          # empty = the stored password


class FlexSettings(FlexCreds):
    enabled: bool = False
    program: str = ""
    fmg_config: Optional[int] = None
    fgt_config: Optional[int] = None
    folder: str = ""                            # Asset Management folder for new licenses ("" = FortiFlex default)


def _flex_cfg():
    conn = db.connect(DATA_DIR)
    cfg = json.loads(db.get_secret(conn, "fortiflex_cfg") or "{}")
    return conn, {"user": db.get_secret(conn, "fortiflex_user") or "", "enabled": bool(cfg.get("enabled")),
                  "program": cfg.get("program", ""), "fmg_config": cfg.get("fmg_config"), "fgt_config": cfg.get("fgt_config"),
                  "folder": cfg.get("folder", "")}


def _flex_client(user="", password=""):
    from .fortiflex import Client
    conn, cfg = _flex_cfg()
    user, password = user or cfg["user"], password or db.get_secret(conn, "fortiflex_password") or ""
    if not user or not password:
        raise HTTPException(400, detail="FortiFlex: API user and password are not set yet (FortiFlex page)")
    return Client(user, password)


def _flex_call(fn):
    from .fortiflex import FlexError
    try:
        return fn()
    except FlexError as e:
        raise HTTPException(400, detail=str(e))


@app.get("/api/fortiflex")
def fortiflex_get():
    conn, cfg = _flex_cfg()
    return dict(cfg, has_password=bool(db.get_secret(conn, "fortiflex_password")))


@app.put("/api/fortiflex")
def fortiflex_put(f: FlexSettings):
    from .fortiflex import check_folder
    try:
        folder = check_folder(f.folder)
    except ValueError as e:
        raise HTTPException(422, detail=[str(e)])
    conn = db.connect(DATA_DIR)
    db.put_secret(conn, "fortiflex_user", f.user.strip())
    if f.password:
        db.put_secret(conn, "fortiflex_password", f.password)
    db.put_secret(conn, "fortiflex_cfg", json.dumps({"enabled": f.enabled, "program": f.program.strip(),
                                                     "fmg_config": f.fmg_config, "fgt_config": f.fgt_config,
                                                     "folder": folder}))
    return fortiflex_get()


@app.post("/api/fortiflex/test")
def fortiflex_test(c: FlexCreds):
    cl = _flex_client(c.user.strip(), c.password)
    return {"programs": _flex_call(cl.programs)}


class FlexConfigsReq(FlexCreds):
    program: str


@app.post("/api/fortiflex/configs")
def fortiflex_configs(c: FlexConfigsReq):
    cl = _flex_client(c.user.strip(), c.password)          # typed (not yet saved) credentials, else the stored ones
    return [{"id": x["id"], "name": x["name"], "status": x.get("status"), "product": (x.get("productType") or {}).get("name", ""),
             "parameters": {p["name"]: p["value"] for p in x.get("parameters") or []}}
            for x in _flex_call(lambda: cl.configs(c.program))]


class FlexFoldersReq(FlexCreds):
    program: str = ""


@app.post("/api/fortiflex/folders")
def fortiflex_folders(c: FlexFoldersReq):
    """Asset folders of the account that owns the chosen program (Asset Management API v3)."""
    cl = _flex_client(c.user.strip(), c.password)
    acc = next((p.get("accountId") for p in _flex_call(cl.programs) if p.get("serialNumber") == c.program), None)
    return {"folders": _flex_call(lambda: cl.asset_folders(acc))}


def _tool_licenses(cl, cfg):
    from .fortiflex import PREFIX
    out = []
    for cid in {cfg["fmg_config"], cfg["fgt_config"]} - {None}:
        for e in _flex_call(lambda cid=cid: cl.entitlements(cid)):
            if str(e.get("description", "")).startswith(PREFIX + " "):
                out.append({k: e.get(k) for k in ("serialNumber", "description", "configId", "status", "tokenStatus",
                                                   "startDate", "endDate")})
    return sorted(out, key=lambda e: e["description"])


@app.get("/api/fortiflex/entitlements")
def fortiflex_entitlements():
    _, cfg = _flex_cfg()
    return {"entitlements": _tool_licenses(_flex_client(), cfg)}


@app.post("/api/fortiflex/stop/{sn}")
def fortiflex_stop(sn: str):
    _, cfg = _flex_cfg()
    cl = _flex_client()
    if sn not in {e["serialNumber"] for e in _tool_licenses(cl, cfg)}:
        raise HTTPException(404, detail=f"{sn} is not a licence of this tool (description 'fgt-sdwan ...')")
    e = _flex_call(lambda: cl.stop(sn))
    return {"serialNumber": e.get("serialNumber"), "status": e.get("status")}     # never the token


def _flex_devices(topo, r, cfg):
    """Devices that need a FortiFlex token in this run: the FortiManager (method day0/ssh) and the FortiGates (idem)."""
    _apply_scope(r)
    mf, mg = _method(r.bootstrap.fmg), _method(r.bootstrap.fgt)
    devs = []
    if mf in ("day0", "ssh") and cfg["fmg_config"]:
        devs.append({"name": topo["fmgr"].get("name") or "fmg", "serial": "", "config_id": cfg["fmg_config"], "role": "fmg"})
    if mg in ("day0", "ssh") and cfg["fgt_config"]:
        devs += [{"name": d["name"], "serial": d.get("serial", ""), "config_id": cfg["fgt_config"], "role": "fgt"}
                 for d in topo["hubs"] + topo["edges"]]
    return devs


def _flex_plan(topo, r):
    from .fortiflex import plan
    _, cfg = _flex_cfg()
    if not cfg["enabled"]:
        return None, []
    devs = _flex_devices(topo, r, cfg)
    if not devs:
        return None, []
    cl = _flex_client()
    ents = {cid: _flex_call(lambda cid=cid: cl.entitlements(cid)) for cid in {d["config_id"] for d in devs}}
    return cl, plan(devs, ents, topo["adom"])


@app.post("/api/fortiflex/check")
def fortiflex_check(r: DeployReq):
    from .fortiflex import ACTION_TEXT, resolve
    topo = model.merged(r.topology)
    cl, rows = _flex_plan(topo, r)
    res = resolve(cl, rows, dry=True) if rows else {}
    return {"devices": [{"name": x["name"], "serial": res[x["name"]]["serial"], "action": x["action"],
                         "text": ACTION_TEXT[x["action"]]} for x in rows]}


_FLEX_LOCK = threading.Lock()
_NOLOCK = __import__("contextlib").nullcontext()                   # two deploys at once must not both create / regenerate a license
DRY_TOKEN = "FORTIFLEXDRYRUN000000"             # placeholder: lets validation pass without touching FortiFlex


def _licenses(topo, r, dry):
    """Serial numbers into the topology, tokens into the job secrets. Returns [(status, message)] for the 'licenses'
    step. dry=True: lookup only, placeholder serials for new licenses and placeholder tokens (used to validate the
    whole request first; nothing is created or regenerated)."""
    from .fortiflex import ACTION_TEXT, FlexError, resolve
    with _FLEX_LOCK if not dry else _NOLOCK:
        cl, rows = _flex_plan(topo, r)
        if not rows:
            return []
        try:
            res = resolve(cl, rows, dry=dry, folder=_flex_cfg()[1]["folder"])
        except FlexError as e:
            raise HTTPException(422, detail=[str(e)])
    if dry:
        for v in res.values():
            v["token"] = DRY_TOKEN
    log = []
    fmg_name = topo["fmgr"].get("name") or "fmg"
    for d in topo["hubs"] + topo["edges"]:
        if d["name"] in res:
            d["serial"] = res[d["name"]]["serial"]
            if res[d["name"]]["token"]:
                r.secrets.fgt_tokens[d["name"]] = res[d["name"]]["token"]
    if fmg_name in res and res[fmg_name]["token"]:
        r.secrets.fmg_token = res[fmg_name]["token"]
    for x in rows:
        log.append(("running", f"{x['name']}: {res[x['name']]['serial']} - {ACTION_TEXT[x['action']]}"
                               + (" (dry run: nothing created)" if dry else "")))
    return log


# ---- cleanup: everything the tool deployed on Proxmox (never the deploy container) ---------------------------------
class CleanupReq(BaseModel):
    server_id: int
    node: str
    confirm: str = ""
    stop_licences: bool = False
    only: Optional[list] = None                 # names from the overview the user saw: nothing else is deleted


_CLEANUP_LOCK = threading.Lock()                # one cleanup at a time, and never during a deployment


def _cleanup_pve(c):
    from .provision_vms import make_pve
    row = db.get_server(db.connect(DATA_DIR), c.server_id)
    if row is None:
        raise HTTPException(404, detail="Proxmox server not found")
    return make_pve({"url": row["url"], "token_id": row["token_id"], "verify_tls": bool(row["verify_tls"])},
                    row["token_secret"])


def _licences_for(names):
    """Tool licences (ACTIVE/PENDING) of exactly these devices ('fgt-sdwan <ADOM> <name>'), so another deployment's
    licences are never stopped."""
    _, cfg = _flex_cfg()
    if not (db.get_secret(db.connect(DATA_DIR), "fortiflex_password") and cfg["user"]):
        return None, []
    cl = _flex_client()
    return cl, [e for e in _tool_licenses(cl, cfg) if e.get("status") in ("ACTIVE", "PENDING")
                and str(e.get("description", "")).split(" ")[-1] in names]


@app.post("/api/cleanup/plan")
def cleanup_plan(c: CleanupReq):
    from . import cleanup
    from .pve import PveError
    try:
        items = cleanup.plan(_cleanup_pve(c), c.node)
    except PveError as e:
        raise HTTPException(502, detail=str(e))
    lic = []
    if c.stop_licences:
        _, lic = _licences_for({i["name"] for i in items if i["kind"] == "vm"})
    return {"items": items, "licences": [{k: e.get(k) for k in ("serialNumber", "description", "status")} for e in lic]}


@app.post("/api/cleanup")
def cleanup_run(c: CleanupReq):
    from . import cleanup
    from .pve import PveError
    if c.confirm != "DELETE":
        raise HTTPException(422, detail=["type DELETE to confirm"])
    if any(j.get("state") == "running" and not j.get("dry_run") for j in JOBS.values()):
        raise HTTPException(409, detail=["a deployment is running: wait until it is done"])
    if not _CLEANUP_LOCK.acquire(blocking=False):
        raise HTTPException(409, detail=["a cleanup is already running"])
    try:
        pve, log = _cleanup_pve(c), []
        try:
            results = cleanup.run(pve, c.node, cleanup.plan(pve, c.node), log.append,
                                  only=set(c.only) if c.only is not None else None)
        except PveError as e:
            raise HTTPException(502, detail=str(e))
        gone_ct = lambda n: any(r["kind"] == "ct" and r["ok"] and (r["name"] == n or r["name"].endswith("-" + n)) for r in results)
        if gone_ct("wan-emu"):
            conn = db.connect(DATA_DIR)                      # the WAN emulation page no longer points at a deleted wan-emu
            for k in ("wanemu_addr", "wanemu_forwards"):
                db.put_secret(conn, k, "")
        cr = _creds()                                        # remote access: forget logins of deleted devices
        fmn = (cr.get("fmg") or {}).get("name")
        if fmn and any(r["kind"] == "vm" and r["ok"] and (r["name"] == fmn or r["name"].endswith("-" + fmn)) for r in results):
            cr.pop("fmg", None)
        if gone_ct("wan-emu"):
            cr.pop("fgt", None)
        db.put_secret(db.connect(DATA_DIR), "access_creds", json.dumps(cr) if cr else "")
        if gone_ct("trafficgen"):
            conn = db.connect(DATA_DIR)                      # the Traffic tab no longer points at a deleted Traffic-Gen
            for k in ("trafficgen_addr", "trafficgen_password"):
                db.put_secret(conn, k, "")
        if c.stop_licences:                                  # only the licences of devices that are really gone now
            gone = {r["name"] for r in results if r["kind"] == "vm" and r["ok"]}
            try:
                cl, lic = _licences_for(gone)
                for e in lic:
                    name = f"licence {e.get('serialNumber')} ({e.get('description', '')})"
                    try:
                        cl.stop(e["serialNumber"])
                        results.append({"name": name, "kind": "licence", "ok": True})
                    except Exception as x:
                        results.append({"name": name, "kind": "licence", "ok": False, "error": str(x)})
            except HTTPException as x:
                results.append({"name": "FortiFlex licences", "kind": "licence", "ok": False, "error": str(x.detail)})
        return {"results": results}
    finally:
        _CLEANUP_LOCK.release()


@app.post("/api/deploy")
def deploy(r: DeployReq):
    if _CLEANUP_LOCK.locked():
        raise HTTPException(409, detail=["a cleanup is running: wait until it is done"])
    topo = model.merged(r.topology)
    sid = topo["pve"].get("server_id")
    if sid:                                    # saved server: url/token-id/secret come from the database
        row = db.get_server(db.connect(DATA_DIR), sid)
        if row is None:
            raise HTTPException(422, detail=["Proxmox server not found (was it deleted?)"])
        topo["pve"].update(url=row["url"], token_id=row["token_id"], verify_tls=bool(row["verify_tls"]))
        r.secrets.pve_token_secret = row["token_secret"]
    _apply_scope(r)
    # FortiFlex: first validate the whole request with placeholders (lookup only), and only then create / reactivate /
    # regenerate licenses, so a refused request never costs points or invalidates a running device's token
    lic = _licenses(topo, r, dry=True)
    if r.scope == "lan":                       # only the LAN clients: no FortiManager templates are rendered
        errs = [e for e in model.validate(topo, devices=False, fmg_vm=False) if not e.startswith("FortiManager")]
        if errs:
            raise HTTPException(422, detail=errs)
        render = lambda: (None, None, {})
    else:
        render = lambda: _render(topo, create_psk=True, devices=r.scope not in ("fmg", "tpl"), fmg_vm=r.scope in ("all", "fmg"))
    _, _, cfg = render()
    _check_bootstrap(topo, r)
    if not r.dry_run:
        _check_bridges(topo, r)                # before FortiFlex creates anything: a wrong bridge costs no points
    if lic and not r.dry_run:
        lic = _licenses(topo, r, dry=False)    # real serials + tokens (under a lock), then render with the real serials
        _, _, cfg = render()
    if sid:                                    # remember this server's choices for the next run
        db.save_last_used(db.connect(DATA_DIR), sid, topo["pve"], topo.get("wanemu"))
    s = r.secrets
    mask = Masker(s.fmg_token, s.fmg_initial_pw, s.fgt_initial_pw, s.fgt_new_pw, s.client_pw, s.tg_pw, r.fmg.password, r.fmg.api_token,
                  s.pve_token_secret, *s.fgt_tokens.values())
    img = {}
    for role in ("fmg", "fgt"):
        if r.images.get(role):
            img[role] = images.get_image(DATA_DIR, r.images[role])["path"]
    boot = {"dry": r.dry_run, "fmg": _method(r.bootstrap.fmg), "fgt": _method(r.bootstrap.fgt),
            "scope": r.scope, "images": img, "pve_secret": s.pve_token_secret,
            "fmg_token": s.fmg_token, "fgt_tokens": dict(s.fgt_tokens),
            "fmg_user": s.admin_user, "fgt_user": s.admin_user,
            "fmg_initial_pw": s.fmg_initial_pw, "fmg_new_pw": r.fmg.password or "",
            "fgt_initial_pw": s.fgt_initial_pw, "fgt_new_pw": s.fgt_new_pw, "client_pw": s.client_pw, "tg_pw": s.tg_pw}
    if not r.dry_run:                                  # remote access page: admin logins (database 0600)
        cr = _creds()
        if r.fmg.password and topo["fmgr"].get("ip"):
            cr["fmg"] = {"name": topo["fmgr"].get("name") or "FortiManager", "host": model.split_cidr(topo["fmgr"]["ip"])[0],
                         "user": s.admin_user or "admin", "password": r.fmg.password}
        if boot["fgt"] == "day0" and r.scope in ("all", "fgt") and s.fgt_new_pw:
            cr["fgt"] = {"user": s.admin_user or "admin", "password": s.fgt_new_pw}
        db.put_secret(db.connect(DATA_DIR), "access_creds", json.dumps(cr))
    if not r.dry_run and topo.get("trafficgen", {}).get("enabled") and \
            (r.scope == "lan" or (boot["fgt"] == "day0" and r.scope in ("all", "fgt"))):
        conn = db.connect(DATA_DIR)                    # where the Traffic tab finds it (password: like the agent token)
        db.put_secret(conn, "trafficgen_addr", f"{model.split_cidr(topo['trafficgen']['ip'])[0]}:8443")
        db.put_secret(conn, "trafficgen_password", s.tg_pw)
    if r.scope == "lan" and not r.dry_run and topo.get("trafficgen", {}).get("enabled"):
        boot["wanemu_secrets"] = _wanemu_secrets()     # Traffic-Gen is installed over SSH with the tool's key
    if boot["fgt"] == "day0" and r.scope not in ("fmg", "tpl", "dev") and not r.dry_run:
        boot["wanemu_secrets"] = _wanemu_secrets()
        w = topo["wanemu"]                     # where the WAN-emulatie page finds the agent of this deployment
        db.put_secret(db.connect(DATA_DIR), "wanemu_addr", f"{model.split_cidr(w['uplink']['ip'])[0]}:{w['agent_port']}")
        from .wanemu.net import forwards
        db.put_secret(db.connect(DATA_DIR), "wanemu_forwards",       # GUI/SSH links on the WAN-emulatie page
                      json.dumps({"host": model.split_cidr(w["uplink"]["ip"])[0], "forwards": forwards(topo)}))
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "state": "running", "dry_run": r.dry_run, "events": [], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "title": f"Deployment of {topo['adom']}" + SCOPE_TITLE[r.scope] + (" (dry-run)" if r.dry_run else ""),
           "steps": {i: "pending" for i, _, _ in STEPS}}
    now = time.strftime("%H:%M:%S")
    for st, m in lic:
        job["events"].append({"t": now, "step": "licenses", "status": st, "msg": mask(m)})
    job["events"].append({"t": now, "step": "licenses", "status": "ok" if lic else "skipped",
                          "msg": f"{len(lic)} licence(s) via FortiFlex" if lic else "FortiFlex not used (FortiFlex page)"})
    job["steps"]["licenses"] = "ok" if lic else "skipped"
    JOBS[jid] = job
    _persist(job)
    threading.Thread(target=_run, args=(job, topo, cfg, r.fmg, r.dry_run, boot, mask), daemon=True).start()
    return {"job": jid}


@app.on_event("startup")
def _startup():
    _jobdb()                                            # also marks jobs a restart killed as interrupted


@app.get("/api/jobs")
def jobs_list():
    """Recent deployments (newest first); the UI uses this to re-attach to the progress after a refresh."""
    with _JOB_LOCK:
        rows = db.list_jobs(_jobdb(), 10)
    for r in rows:                                      # a job that is running right now is more current than its stored row
        if r["id"] in JOBS:
            r["state"] = JOBS[r["id"]]["state"]
    return {"latest": rows[0]["id"] if rows else None, "jobs": rows}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    j = _get_job(jid)
    if j is None:
        raise HTTPException(404, detail="This deployment does not exist (any more)")
    return j


@app.get("/api/jobs/{jid}/stream")
def stream(jid: str):
    if _get_job(jid) is None:
        raise HTTPException(404, detail="This deployment does not exist (any more)")

    def gen():
        sent = 0
        while True:
            j = JOBS.get(jid) or _get_job(jid)
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
