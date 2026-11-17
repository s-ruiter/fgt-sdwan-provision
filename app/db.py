"""Persistent settings: Proxmox servers (incl. API token secret) and the last-used Proxmox choices per server.

SQLite file in DATA_DIR, mode 0600 (directory 0700). The token secret is stored in plain text, exactly like
Spawnpad does: the file is the only protection, and it is never returned by the API.
"""
import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    token_id TEXT NOT NULL,
    token_secret TEXT NOT NULL,
    verify_tls INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS last_used (
    server_id INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
# keys of topology['pve'] that are remembered per server (never the url/token/secret themselves)
REMEMBER = ("node", "disk_storage", "import_storage", "iso_storage", "default_bridge", "cpu_type", "license_mode",
            "ports", "fmg", "fgt")


def connect(data_dir):
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / "fgt-sdwan.db"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def get_server(conn, server_id):
    return conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()


def save_last_used(conn, server_id, pve_cfg):
    data = {k: pve_cfg[k] for k in REMEMBER if k in pve_cfg}
    conn.execute("INSERT INTO last_used(server_id,data) VALUES(?,?) ON CONFLICT(server_id) DO UPDATE SET data=excluded.data",
                 (server_id, json.dumps(data)))
    conn.execute("INSERT INTO settings(key,value) VALUES('last_server',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (str(server_id),))
    conn.commit()


def get_last_used(conn, server_id):
    row = conn.execute("SELECT data FROM last_used WHERE server_id=?", (server_id,)).fetchone()
    return json.loads(row["data"]) if row else {}


def last_server(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='last_server'").fetchone()
    return int(row["value"]) if row else None
