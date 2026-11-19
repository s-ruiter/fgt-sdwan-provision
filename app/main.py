import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, Union

from . import cloudinit, csvio, db, images, model
from .bootstrap import Masker
from .pve import Pve, PveError
from .fmg import Fmg, FmgError, pipeline
from . import fmgtemplates

from .config import DATA_DIR, ORCH_DIR
from .routes_servers import router as servers_router
STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="FGT SD-WAN Provision")
app.include_router(servers_router)
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
    ("licenses", "Licenties", "FortiFlex: per device een bestaande licentie hergebruiken (nieuw token) of een nieuwe aanmaken; serienummer en token gaan naar de uitrol"),
    ("pve_fmg", "FortiManager-VM", "Image naar Proxmox, VM + datadisk + Day-0 config-drive (cloud-init) aanmaken en starten"),
    ("bootstrap_fmg", "FortiManager ZTP", "Vast IP, route, DNS en FortiFlex-licentie: via Day-0 (wachten) of via SSH (execute vm-license)"),
    ("preflight", "Preflight", "Verbinding en FortiManager-versie (8.0.x) controleren"),
    ("fmg_settings", "FortiManager-instellingen", "Uitlogtimers 180 min, tijdzone Europe/Amsterdam, ADOMs in advanced mode, FortiAnalyzer-features aan (herstart)"),
    ("adom", "ADOM", "ADOM aanmaken op basis van jouw naam (bestaat hij al: hergebruiken)"),
    ("devices", "Model devices", "2 hubs + 3 branches toevoegen met opgegeven serienummers"),
    ("templates", "Templates", "Metadata-variabelen, system-, SD-WAN- en CLI-templates, policy packages, template groups en blueprints in de ADOM"),
    ("pve_wan", "WAN-emulator", "LXC wan-emu: routeert ISP-HUB / ISP-A / ISP-B (NAT naar het lab), DHCP voor de spokes, verkeersmanipulatie per link"),
    ("pve_fgt", "FortiGate-VM's", "Image naar Proxmox, per FortiGate een VM met Day-0 ISO (wan1: hub vast IP + gateway, branch DHCP; DNS, FMG-koppeling, FortiFlex-licentie) starten"),
    ("bootstrap_fgt", "FortiGate ZTP", "Day-0: wachten tot ze gelicenseerd online zijn in FMG; of via SSH op bestaande VM's"),
    ("install", "Install", "Quick install (device settings) per device, daarna Re-install policy per rol; echte FortiGates pas als ze online zijn"),
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
    scope: str = "all"                         # 'all', 'fmg' (only FortiManager), 'fgt' (only FortiGates), 'tpl' (only templates), 'dev' (only model devices)


SCOPE_TITLE = {"all": "", "fmg": " - alleen FortiManager", "fgt": " - alleen FortiGates", "tpl": " - alleen templates", "dev": " - alleen model devices"}


def _apply_scope(r):
    """The scope wins over the per-product method: a product that is not deployed gets method 'none'."""
    if r.scope not in SCOPE_TITLE:
        raise HTTPException(422, detail=[f"onbekende uitrol-keuze '{r.scope}' (all, fmg, fgt, tpl of dev)"])
    if r.scope == "fgt":
        r.bootstrap.fmg = "none"
    elif r.scope == "fmg":
        r.bootstrap.fgt = "none"
    elif r.scope in ("tpl", "dev"):            # existing FortiManager, no VMs at all
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
        raise HTTPException(500, detail=[f"Templates genereren mislukt: {type(e).__name__}: {e}"])
    return project, inv, plan


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
        if not topo["pve"].get("server_id") and not topo["pve"].get("url"):
            errs.append("Proxmox: kies een server (menu Servers) of voeg er een toe")
        else:
            errs += model.validate_pve(topo, wanemu=mg == "day0" and r.scope not in ("fmg", "tpl", "dev"))
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
    if not r.fmg.password and not r.fmg.api_token:     # the tool always logs in to the FortiManager API
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
    mode = cloudinit.license_mode("fmg" if r.device == "fmg" else "fgt", topo["pve"].get("license_mode", "cli"))
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
    project, inv, plan = _render(topo)
    cfg = fmgtemplates.preview(plan)
    return {"project": project, "inventory": inv, "configs": cfg, "warnings": plan["warnings"],
            "roles": [{"name": r["name"], "kind": r["kind"], "devices": r["devices"]} for r in plan["roles"]],
            "lines": {k: len(v.splitlines()) for k, v in cfg.items()}}


class ExportReq(BaseModel):
    topology: dict
    full: bool = False                     # True: settings + devices (reusable deployment); False: devices only
    tokens: dict = {}                      # {"fmg": token, "devices": {name: token}}: FortiFlex tokens typed in the GUI


@app.post("/api/csv/export")
def csv_export(r: ExportReq):
    """Current form state as CSV. Contains the FortiFlex tokens (sent along), never admin passwords or the Proxmox secret."""
    topo = model.merged(r.topology)
    name = ""
    sid = (topo.get("pve") or {}).get("server_id")
    if r.full and sid:
        row = db.get_server(db.connect(DATA_DIR), sid)
        name = row["name"] if row else ""
    body = csvio.export_csv(topo, full=r.full, server_name=name, tokens=r.tokens)
    fn = "sdwan-volledig.csv" if r.full else "sdwan-devices.csv"
    return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/csv/import")
async def csv_import(file: UploadFile = File(...), topology: str = Form("{}"), apply_settings: str = Form("")):
    """Devices; with apply_settings also [algemeen]/[vrf]/[proxmox]. The current GUI settings are sent along for validation."""
    raw = await file.read()
    if len(raw) > 1_000_000:
        raise HTTPException(413, detail="CSV-bestand te groot")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")                      # Excel 'CSV (door lijst gescheiden)'
    try:
        current = json.loads(topology or "{}")
    except ValueError:
        raise HTTPException(422, detail="CSV: ongeldige topologie in het verzoek")
    try:
        res = csvio.import_devices(text, current, bool(apply_settings))
    except csvio.CsvError as e:
        raise HTTPException(422, detail=f"CSV: {e}")
    res["summary"] = f"{len(res['hubs'])} hub(s), {len(res['edges'])} branch(es)" + (" + instellingen" if res["settings"] else "")
    return res


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
        _persist(job)

    def log(level, msg):
        job["events"].append({"t": time.strftime("%H:%M:%S"), "step": None, "status": level, "msg": mask(msg)})

    host = fmgcfg.host or model.split_cidr(topo["fmgr"]["ip"])[0]
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
        raise HTTPException(409, detail="Nog geen WAN-emulator uitgerold: rol eerst FortiGates uit (Alles of Alleen FortiGates)")
    return Agent(addr, tok)


def _agent_call(fn):
    from .wanemu.client import AgentError
    try:
        return fn()
    except AgentError as e:
        raise HTTPException(e.status, detail=str(e))


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
        raise HTTPException(404, detail=f"onbekende preset {name}")
    a = _agent()
    changes = preset_changes(name, _agent_call(a.links), link)
    for lid, vals in changes:
        _agent_call(lambda lid=lid, vals=vals: a.put(lid, vals))
    return {"applied": [lid for lid, _ in changes]}


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


def _flex_cfg():
    conn = db.connect(DATA_DIR)
    cfg = json.loads(db.get_secret(conn, "fortiflex_cfg") or "{}")
    return conn, {"user": db.get_secret(conn, "fortiflex_user") or "", "enabled": bool(cfg.get("enabled")),
                  "program": cfg.get("program", ""), "fmg_config": cfg.get("fmg_config"), "fgt_config": cfg.get("fgt_config")}


def _flex_client(user="", password=""):
    from .fortiflex import Client
    conn, cfg = _flex_cfg()
    user, password = user or cfg["user"], password or db.get_secret(conn, "fortiflex_password") or ""
    if not user or not password:
        raise HTTPException(400, detail="FortiFlex: API-gebruiker en wachtwoord zijn nog niet ingesteld (pagina FortiFlex)")
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
    conn = db.connect(DATA_DIR)
    db.put_secret(conn, "fortiflex_user", f.user.strip())
    if f.password:
        db.put_secret(conn, "fortiflex_password", f.password)
    db.put_secret(conn, "fortiflex_cfg", json.dumps({"enabled": f.enabled, "program": f.program.strip(),
                                                     "fmg_config": f.fmg_config, "fgt_config": f.fgt_config}))
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
        raise HTTPException(404, detail=f"{sn} is geen licentie van deze tool (omschrijving 'fgt-sdwan ...')")
    return _flex_call(lambda: cl.stop(sn))


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


def _licenses(topo, r):
    """At deployment, before validation: serial numbers into the topology, tokens into the job secrets.
    Returns [(status, message)] for the 'licenses' step. Dry run: lookup only."""
    from .fortiflex import ACTION_TEXT, FlexError, resolve
    cl, rows = _flex_plan(topo, r)
    if not rows:
        return []
    try:
        res = resolve(cl, rows, dry=r.dry_run)
    except FlexError as e:
        raise HTTPException(422, detail=[str(e)])
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
                               + (" (dry-run: niets aangemaakt)" if r.dry_run else "")))
    return log


@app.post("/api/deploy")
def deploy(r: DeployReq):
    topo = model.merged(r.topology)
    sid = topo["pve"].get("server_id")
    if sid:                                    # saved server: url/token-id/secret come from the database
        row = db.get_server(db.connect(DATA_DIR), sid)
        if row is None:
            raise HTTPException(422, detail=["Proxmox-server niet gevonden (is hij verwijderd?)"])
        topo["pve"].update(url=row["url"], token_id=row["token_id"], verify_tls=bool(row["verify_tls"]))
        r.secrets.pve_token_secret = row["token_secret"]
    _apply_scope(r)
    lic = _licenses(topo, r)                   # FortiFlex: serial numbers + tokens, before validation needs them
    _, _, cfg = _render(topo, create_psk=True, devices=r.scope not in ("fmg", "tpl"), fmg_vm=r.scope in ("all", "fmg"))
    _check_bootstrap(topo, r)
    if sid:                                    # remember this server's choices for the next run
        db.save_last_used(db.connect(DATA_DIR), sid, topo["pve"])
    s = r.secrets
    mask = Masker(s.fmg_token, s.fmg_initial_pw, s.fgt_initial_pw, s.fgt_new_pw, r.fmg.password, r.fmg.api_token,
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
            "fgt_initial_pw": s.fgt_initial_pw, "fgt_new_pw": s.fgt_new_pw}
    if boot["fgt"] == "day0" and r.scope not in ("fmg", "tpl", "dev") and not r.dry_run:
        boot["wanemu_secrets"] = _wanemu_secrets()
        w = topo["wanemu"]                     # where the WAN-emulatie page finds the agent of this deployment
        db.put_secret(db.connect(DATA_DIR), "wanemu_addr", f"{model.split_cidr(w['uplink']['ip'])[0]}:{w['agent_port']}")
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "state": "running", "dry_run": r.dry_run, "events": [], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "title": f"Uitrol van {topo['adom']}" + SCOPE_TITLE[r.scope] + (" (dry-run)" if r.dry_run else ""),
           "steps": {i: "pending" for i, _, _ in STEPS}}
    now = time.strftime("%H:%M:%S")
    for st, m in lic:
        job["events"].append({"t": now, "step": "licenses", "status": st, "msg": mask(m)})
    job["events"].append({"t": now, "step": "licenses", "status": "ok" if lic else "skipped",
                          "msg": f"{len(lic)} licentie(s) via FortiFlex" if lic else "FortiFlex niet gebruikt (pagina FortiFlex)"})
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
        raise HTTPException(404, detail="Deze uitrol bestaat niet (meer)")
    return j


@app.get("/api/jobs/{jid}/stream")
def stream(jid: str):
    if _get_job(jid) is None:
        raise HTTPException(404, detail="Deze uitrol bestaat niet (meer)")

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
