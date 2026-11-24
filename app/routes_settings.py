"""Settings: export / import a JSON file with the whole topology, and saved settings (with a description) in the tool."""
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from . import config, db, settings_io

router = APIRouter(prefix="/api/settings")


class ExportIn(BaseModel):
    topology: dict
    ui: dict = {}


class SaveIn(BaseModel):
    description: str
    topology: dict
    ui: dict = {}
    passwords: dict = {}                       # kept with a save in the tool (database 0600), never in an export


PASSWORDS = ("fmg_pass", "fgt_newpw", "client_pw", "tg_pw")     # form fields whose value may be kept


def _with_passwords(f, pw):
    f["passwords"] = {k: str(v) for k, v in (pw or {}).items() if k in PASSWORDS and v}
    return f


def conn():
    return db.connect(config.DATA_DIR)


def _server_name(c, topology):
    sid = (topology.get("pve") or {}).get("server_id")
    row = db.get_server(c, sid) if sid else None
    return row["name"] if row else ""


def _servers(c):
    return {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM servers").fetchall()}


def _load(c, data):
    try:
        t, ui, notes = settings_io.from_file(data, _servers(c))
    except settings_io.SettingsError as e:
        raise HTTPException(422, detail=str(e))
    return {"topology": t, "ui": ui, "notes": notes}


@router.post("/export")
def export(r: ExportIn):
    c = conn()
    f = settings_io.to_file(r.topology, _server_name(c, r.topology), r.ui)
    adom = re.sub(r"[^A-Za-z0-9_.-]+", "-", f["topology"].get("adom") or "sdwan")
    import json
    return Response(json.dumps(f, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="fgt-sdwan-{adom}.json"'})


@router.post("/import")
def import_(data: dict):
    return _load(conn(), data)


def _desc(d):
    d = (d or "").strip()
    if not d or len(d) > 200:
        raise HTTPException(422, detail="description is required (at most 200 characters)")
    return d


@router.get("/saved")
def saved_list():
    return db.list_settings(conn())


@router.post("/saved")
def saved_add(r: SaveIn):
    c = conn()
    f = _with_passwords(settings_io.to_file(r.topology, _server_name(c, r.topology), r.ui), r.passwords)
    s = settings_io.summary(f["topology"])
    return {"id": db.save_settings(c, _desc(r.description), s["adom"], s["devices"], f)}


@router.put("/saved/{sid}")
def saved_update(sid: int, r: SaveIn):
    c = conn()
    f = _with_passwords(settings_io.to_file(r.topology, _server_name(c, r.topology), r.ui), r.passwords)
    s = settings_io.summary(f["topology"])
    if db.save_settings(c, _desc(r.description), s["adom"], s["devices"], f, sid) is None:
        raise HTTPException(404, detail="saved settings not found")
    return {"id": sid}


@router.get("/saved/{sid}")
def saved_get(sid: int):
    c = conn()
    row: Optional[dict] = db.get_settings(c, sid)
    if row is None:
        raise HTTPException(404, detail="saved settings not found")
    return dict(_load(c, row["data"]), id=sid, description=row["description"],
                passwords=row["data"].get("passwords") or {})


@router.delete("/saved/{sid}")
def saved_delete(sid: int):
    if not db.delete_settings(conn(), sid):
        raise HTTPException(404, detail="saved settings not found")
    return {"ok": True}
