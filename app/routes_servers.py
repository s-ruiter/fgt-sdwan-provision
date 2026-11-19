"""CRUD for saved Proxmox servers. The token secret is write-only: it is never returned."""
import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from . import config, db
from .pve import Pve, PveError

router = APIRouter(prefix="/api/servers")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
URL = re.compile(r"^https?://[^\s/]+(:\d+)?/?$")
TOKEN_ID = re.compile(r"^[^\s!@]+@[^\s!@]+![^\s!]+$")


class ServerIn(BaseModel):
    name: str
    url: str
    token_id: str
    token_secret: Optional[str] = None
    verify_tls: bool = True


class TestIn(BaseModel):
    node: Optional[str] = None


def conn():
    return db.connect(config.DATA_DIR)


def _out(row, last):
    return {"id": row["id"], "name": row["name"], "url": row["url"], "token_id": row["token_id"],
            "verify_tls": bool(row["verify_tls"]), "has_secret": True, "last": row["id"] == last}


def _check(body):
    if not NAME.match(body.name):
        raise HTTPException(422, "Name: 1-40 characters (letters, digits, space, _ . -)")
    if not URL.match(body.url.strip()):
        raise HTTPException(422, "URL as https://host:8006")
    if not TOKEN_ID.match(body.token_id.strip()):
        raise HTTPException(422, "Token ID as user@realm!name")


def client(row):
    return Pve(row["url"], row["token_id"], row["token_secret"], bool(row["verify_tls"]), timeout=15)


@router.get("")
def list_servers():
    c = conn()
    last = db.last_server(c)
    return [_out(r, last) for r in c.execute("SELECT * FROM servers ORDER BY name")]


@router.post("", status_code=201)
def create_server(body: ServerIn):
    _check(body)
    if not body.token_secret:
        raise HTTPException(422, "Token secret is required")
    c = conn()
    try:
        cur = c.execute("INSERT INTO servers(name,url,token_id,token_secret,verify_tls) VALUES(?,?,?,?,?)",
                        (body.name.strip(), body.url.strip().rstrip("/"), body.token_id.strip(), body.token_secret,
                         int(body.verify_tls)))
    except sqlite3.IntegrityError:
        c.rollback()
        c.close()
        raise HTTPException(409, f"A server named '{body.name}' already exists")
    c.commit()
    out = _out(db.get_server(c, cur.lastrowid), None)
    c.close()
    return out


@router.put("/{sid}")
def update_server(sid: int, body: ServerIn):
    _check(body)
    c = conn()
    old = db.get_server(c, sid)
    if old is None:
        raise HTTPException(404, "Server not found")
    url, tid = body.url.strip().rstrip("/"), body.token_id.strip()
    if not body.token_secret and (url != old["url"] or tid != old["token_id"]):
        raise HTTPException(422, "Enter the token secret again when the URL or token ID changes")
    try:
        c.execute("UPDATE servers SET name=?,url=?,token_id=?,token_secret=?,verify_tls=? WHERE id=?",
                  (body.name.strip(), url, tid, body.token_secret or old["token_secret"], int(body.verify_tls), sid))
    except sqlite3.IntegrityError:
        c.rollback()
        c.close()
        raise HTTPException(409, f"A server named '{body.name}' already exists")
    c.commit()
    out = _out(db.get_server(c, sid), db.last_server(c))
    c.close()
    return out


@router.delete("/{sid}", status_code=204)
def delete_server(sid: int):
    c = conn()
    c.execute("DELETE FROM servers WHERE id=?", (sid,))
    c.commit()
    return Response(status_code=204)


@router.post("/{sid}/test")
def test_server(sid: int, body: TestIn):
    row = db.get_server(conn(), sid)
    if row is None:
        raise HTTPException(404, "Server not found")
    p = client(row)
    try:
        res = p.check()
        if body.node:
            res["bridges"] = p.bridges(body.node)
        return res
    except PveError as e:
        raise HTTPException(502, e.detail)


@router.get("/{sid}/nodes/{node}/options")
def node_options(sid: int, node: str):
    row = db.get_server(conn(), sid)
    if row is None:
        raise HTTPException(404, "Server not found")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", node):
        raise HTTPException(422, "Ongeldige node")
    try:
        return client(row).node_options(node)
    except PveError as e:
        raise HTTPException(502, e.detail)


@router.get("/{sid}/last-used")
def last_used(sid: int):
    return db.get_last_used(conn(), sid)
