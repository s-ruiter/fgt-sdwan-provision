"""Consoles: encrypted console password, console user per Proxmox server, Proxmox termproxy consoles (LXC via the API
token, VMs via a console user), the /api/console websocket with automatic login."""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import secretbox  # noqa: E402

# ---- secret box: Fernet, key file 0600 in the data folder
d = tempfile.mkdtemp()
tok = secretbox.seal(d, "Console-pw1!")
assert "Console-pw1!" not in tok and secretbox.unseal(d, tok) == "Console-pw1!"
kf = os.path.join(d, "console.key")
assert os.path.exists(kf) and stat.S_IMODE(os.stat(kf).st_mode) == 0o600
key = open(kf, "rb").read()
assert secretbox.unseal(d, secretbox.seal(d, "x")) == "x" and open(kf, "rb").read() == key      # same key reused
for bad_dir, bad_tok in ((tempfile.mkdtemp(), tok), (d, "garbage")):
    try:
        secretbox.unseal(bad_dir, bad_tok)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
print("OK  secretbox: versleuteld, sleutel 0600, hergebruikt, verkeerde sleutel/token -> ValueError")

# ---- console user per Proxmox server: password write-only and encrypted
import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402

c = TestClient(main.app)
base = {"name": "lab", "url": "https://pve.example:8006", "token_id": "root@pam!t", "token_secret": "tok-secret-1",
        "verify_tls": False}
r = c.post("/api/servers", json=dict(base, console_user="fgtsdwan@pve", console_password="Console-pw1!"))
assert r.status_code == 201, r.text
sid = r.json()["id"]
lst = c.get("/api/servers")
assert "Console-pw1!" not in lst.text and "tok-secret-1" not in lst.text
me = [x for x in lst.json() if x["id"] == sid][0]
assert me["console_user"] == "fgtsdwan@pve" and me["has_console_password"] is True
conn = db.connect(os.environ["DATA_DIR"])
raw = db.get_secret(conn, f"pve_console:{sid}")
assert raw and "Console-pw1!" not in raw
assert db.get_console(conn, sid, os.environ["DATA_DIR"]) == {"user": "fgtsdwan@pve", "password": "Console-pw1!"}
r = c.put(f"/api/servers/{sid}", json=dict(base, token_secret=None, console_user="fgtsdwan@pve"))   # no password: kept
assert r.status_code == 200 and db.get_console(conn, sid, os.environ["DATA_DIR"])["password"] == "Console-pw1!"
r = c.put(f"/api/servers/{sid}", json=dict(base, token_secret=None, console_user="other@pve", console_password="New-pw2!"))
assert db.get_console(conn, sid, os.environ["DATA_DIR"]) == {"user": "other@pve", "password": "New-pw2!"}
r = c.put(f"/api/servers/{sid}", json=dict(base, token_secret=None))              # console fields left out: unchanged
assert db.get_console(conn, sid, os.environ["DATA_DIR"])["user"] == "other@pve"
ex = c.post("/api/settings/export", json={"topology": {"pve": {"server_id": sid}}, "ui": {}})
assert ex.status_code == 200 and "New-pw2!" not in ex.text and "other@pve" not in ex.text
r = c.put(f"/api/servers/{sid}", json=dict(base, token_secret=None, console_user=""))           # "" clears
assert db.get_console(conn, sid, os.environ["DATA_DIR"]) is None
assert [x for x in c.get("/api/servers").json() if x["id"] == sid][0]["has_console_password"] is False
c.put(f"/api/servers/{sid}", json=dict(base, token_secret=None, console_user="u@pve", console_password="P-w0rd!x"))
assert c.delete(f"/api/servers/{sid}").status_code == 204 and not db.get_secret(conn, f"pve_console:{sid}")
print("OK  console-gebruiker per server: wachtwoord versleuteld, nooit terug, niet in export, leeg = wissen, weg bij delete")

# ---- open a Proxmox console: LXC with the API token, VM with the console user (PVE refuses VM consoles to a token)
from app import console  # noqa: E402
from tests import fake_proxmox as fp  # noqa: E402

fp.reset()
psrv = fp.start(8087)
try:
    row = {"url": "http://127.0.0.1:8087", "token_id": "root@pam!t", "token_secret": "tsec", "verify_tls": 0}
    S = fp.STATE
    S["cts"]["118"] = {"vmid": 118, "params": {"hostname": "lab1-client-site1-1", "tags": "fgt-sdwan"}, "status": "running"}
    S["vms"]["117"] = {"vmid": 117, "name": "lab1-site1-3", "params": {"tags": "fgt-sdwan", "serial0": "socket"}, "status": "running"}
    S["vms"]["115"] = {"vmid": 115, "name": "lab1-site1-1", "params": {"tags": "fgt-sdwan"}, "status": "running"}
    S["vms"]["116"] = {"vmid": 116, "name": "lab1-site1-2", "params": {"tags": "fgt-sdwan", "serial0": "socket"}, "status": "stopped"}
    S["console_users"] = {"fgtsdwan@pve": "Console-pw1!"}
    cu = {"user": "fgtsdwan@pve", "password": "Console-pw1!"}
    o = console.open_session(row, None, "lab1-client-site1-1")
    assert o["headers"] == {"Authorization": "PVEAPIToken=root@pam!t=tsec"} and o["auth_line"] == "root@pam!t:PVEVNC:abc/def+==\n"
    assert o["ws_url"] == "ws://127.0.0.1:8087/api2/json/nodes/pve1/lxc/118/vncwebsocket?port=5900&vncticket=PVEVNC%3Aabc%2Fdef%2B%3D%3D", o
    o = console.open_session(row, cu, "lab1-site1-3")
    assert o["headers"] == {"Cookie": "PVEAuthCookie=PVE:fgtsdwan@pve:TICKET"} and "/qemu/117/vncwebsocket" in o["ws_url"]
    assert o["auth_line"].startswith("fgtsdwan@pve:") and S["termproxies"][-1] == ("qemu", 117, "fgtsdwan@pve")

    def err(name, cons, text):
        try:
            console.open_session(row, cons, name)
            raise AssertionError(f"expected ConsoleError for {name}")
        except console.ConsoleError as e:
            assert text in str(e), (name, str(e))
    err("lab1-nothing", cu, "not found in Proxmox")
    err("lab1-site1-2", cu, "is not running")
    err("lab1-site1-3", None, "add a console user to the Proxmox server")
    err("lab1-site1-1", cu, "no serial port")
    err("lab1-site1-3", {"user": "fgtsdwan@pve", "password": "wrong"}, "console login refused")
    S["termproxy_403"] = True
    err("lab1-client-site1-1", None, "lacks VM.Console")
    err("lab1-site1-3", cu, "lacks VM.Console")
    S["termproxy_403"] = False
finally:
    psrv.shutdown()
print("OK  console openen: LXC via token, VM via console-gebruiker (cookie+CSRF), duidelijke fouten")
