"""Several FortiManagers on the Access page: one kept login per hostname (a second FortiManager no longer replaces the
first). An older single 'fmg' entry is still read."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from fastapi.testclient import TestClient  # noqa: E402
from app import db, main  # noqa: E402

conn = db.connect(os.environ["DATA_DIR"])
c = TestClient(main.app)
old = {"name": "fmg", "host": "10.10.106.11", "user": "admin", "password": "pw-old"}
db.put_secret(conn, "access_creds", json.dumps({"fmg": old}))                  # stored by an older version

# a deployment of fmg2 adds it next to the old one
main._remember_fmg(main._creds(), {"name": "fmg2", "host": "10.10.106.249", "user": "admin", "password": "pw-new", "vm": "fmg2"})
a = c.get("/api/access").json()
assert [(f["name"], f["ip"], f["key"]) for f in a["fmgs"]] == [("fmg", "10.10.106.11", "@fmg:fmg"), ("fmg2", "10.10.106.249", "@fmg:fmg2")], a["fmgs"]
assert all(f["password"] for f in a["fmgs"]) and "pw-old" not in json.dumps(a) and "pw-new" not in json.dumps(a)
assert c.post("/api/access/password", json={"name": "@fmg:fmg"}).json()["password"] == "pw-old"
assert c.post("/api/access/password", json={"name": "@fmg:fmg2"}).json()["password"] == "pw-new"
assert c.post("/api/access/password", json={"name": "fmg2"}).json()["password"] == "pw-new"
# redeploying fmg replaces only its own entry
main._remember_fmg(main._creds(), dict(old, password="pw-old2", vm="SDW-fmg"))
assert [f["name"] for f in c.get("/api/access").json()["fmgs"]] == ["fmg", "fmg2"]
assert c.post("/api/access/password", json={"name": "@fmg:fmg"}).json()["password"] == "pw-old2"
# the reuse check knows the address of every FortiManager
assert main._fmg_known(main._creds()) == {"fmg": "10.10.106.11", "fmg2": "10.10.106.249"}
# console: the Proxmox name of that FortiManager (its own prefix), never another VM
db.put_secret(conn, "console_target", json.dumps({"server_id": 1, "prefix": ""}))
main.CONSOLE_SERVER = lambda sid: {"url": "x"}
assert main._console_target("@fmg:fmg")[0] == "SDW-fmg" and main._console_target("@fmg:fmg2")[0] == "fmg2"
try:
    main._console_target("@fmg:nope")
    raise SystemExit("unknown FortiManager accepted")
except Exception as e:
    assert "unknown" in str(e).lower(), e
# cleanup of one FortiManager keeps the other
cr = main._forget_fmgs(main._creds(), [{"kind": "vm", "ok": True, "name": "fmg2"}, {"kind": "vm", "ok": False, "name": "SDW-fmg"}])
assert list(cr["fmgs"]) == ["fmg"] and cr["fmg_last"] == "fmg", cr
cr = main._forget_fmgs(cr, [{"kind": "vm", "ok": True, "name": "SDW-fmg"}])
assert "fmgs" not in cr and "fmg_last" not in cr, cr
print("OK  Access: meerdere FortiManagers naast elkaar (login, wachtwoord, console per hostname)")

# the page lists them all
js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "app.js")).read()
assert "j.fmgs" in js and "f.key" in js
print("OK  UI: alle FortiManagers in Remote access")
