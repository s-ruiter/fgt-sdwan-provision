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
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    steps TEXT NOT NULL,
    events TEXT NOT NULL
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


def save_last_used(conn, server_id, pve_cfg, wanemu=None):
    """Remember this server's Proxmox choices, plus the WAN emulator uplink (lab-specific, no default). A run without a
    filled-in uplink keeps the one remembered before."""
    data = {k: pve_cfg[k] for k in REMEMBER if k in pve_cfg}
    up = (wanemu or {}).get("uplink") or {}
    if str(up.get("ip") or "").strip():
        data["wanemu_uplink"] = {k: up.get(k) for k in ("bridge", "vlan", "ip", "gw")}
        data["wanemu_bridge"] = (wanemu or {}).get("bridge")
    else:
        prev = get_last_used(conn, server_id)
        data.update({k: prev[k] for k in ("wanemu_uplink", "wanemu_bridge") if k in prev})
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


PSK_PLACEHOLDER = "<PSK-generated-at-deployment>"


def get_psk(conn, adom, create=False):
    """Random IPsec PSK per ADOM, kept so re-running a deployment does not change the tunnels' key.
    create=False (previews): the stored PSK or a visible placeholder; nothing is generated or stored."""
    import secrets
    row = conn.execute("SELECT value FROM settings WHERE key=?", (f"psk:{adom}",)).fetchone()
    if row:
        return row[0]
    if not create:
        return PSK_PLACEHOLDER
    psk = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"psk:{adom}", psk))
    conn.commit()
    return psk


def get_secret(conn, name, create=None):
    """A tool secret (e.g. the wan-emu SSH key). create = callable that makes a new value when none is stored;
    without it a missing secret returns None."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (f"secret:{name}",)).fetchone()
    if row:
        return row[0]
    if create is None:
        return None
    v = create()
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"secret:{name}", v))
    conn.commit()
    return v


def put_secret(conn, name, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"secret:{name}", value))
    conn.commit()


# ---- deployment jobs: persisted so progress survives a page refresh and a service restart -------------------------
def save_job(conn, job):
    """job = {id, created, title, state, dry_run, steps{}, events[]}; events are already masked (no secrets)."""
    conn.execute("INSERT INTO jobs(id,created,title,state,dry_run,steps,events) VALUES(?,?,?,?,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET state=excluded.state, steps=excluded.steps, events=excluded.events, title=excluded.title",
                 (job["id"], job.get("created", ""), job.get("title", ""), job["state"], int(bool(job.get("dry_run"))),
                  json.dumps(job["steps"]), json.dumps(job["events"])))
    conn.commit()


def _job_row(r):
    return {"id": r["id"], "created": r["created"], "title": r["title"], "state": r["state"], "dry_run": bool(r["dry_run"]),
            "steps": json.loads(r["steps"]), "events": json.loads(r["events"])}


def load_job(conn, jid):
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return _job_row(r) if r else None


def list_jobs(conn, limit=10):
    """Newest first, without events."""
    rows = conn.execute("SELECT id,created,title,state,dry_run FROM jobs ORDER BY created DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "created": r["created"], "title": r["title"], "state": r["state"], "dry_run": bool(r["dry_run"])} for r in rows]


def interrupt_running_jobs(conn, when):
    """At service start: a job still 'running' in the database was killed by a restart. Mark it, never leave it 'running'."""
    n = 0
    for r in conn.execute("SELECT * FROM jobs WHERE state='running'").fetchall():
        j = _job_row(r)
        cur = next((s for s, v in j["steps"].items() if v == "running"), None)
        if cur:
            j["steps"][cur] = "error"
        j["events"].append({"t": when, "step": cur, "status": "error",
                            "msg": "Interrupted: the service restarted while the deployment was running. Check what was already created "
                                   "(VMs on Proxmox, objects in FortiManager) and start the deployment again; existing parts are reused."})
        j["state"] = "failed"
        save_job(conn, j)
        n += 1
    return n
