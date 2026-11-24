"""Settings: export to a JSON file and import it again (the whole topology), and saved settings with a description
inside the tool (database). Never passwords, tokens or the Proxmox secret."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, model, settings_io  # noqa: E402

c = TestClient(main.app)
conn = db.connect(os.environ["DATA_DIR"])
conn.execute("INSERT INTO servers(id,name,url,token_id,token_secret) VALUES(7,'pve01','https://pve:8006','a@pam!b','SECRET-X')")
conn.commit()

# a topology that differs from the defaults in many places
t = model.merged({"adom": "LAB-1", "hubs": None})
t["region"]["multi_vrf"] = True
t["hubs"][0]["serial"] = "FGVMMLTM26000001"
t["edges"] = t["edges"][:2]
t["pve"].update(server_id=7, node="pve01", first_vmid=500, url="https://leak:8006", token_id="x@pam!y")
t["wanemu"].update(bridge="vmbr5", uplink={"bridge": "vmbr0", "vlan": None, "ip": "10.2.199.250/24", "gw": "10.2.199.1"})
t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")

# ---- export: complete, server by name, no server id / url / token id, no secrets
r = c.post("/api/settings/export", json={"topology": t, "ui": {"scope": "fgt", "fmg_user": "admin"}})
assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "LAB-1" in r.headers["content-disposition"]
f = r.json()
assert f["format"] == "fgt-sdwan-settings" and f["version"] == 1 and f["ui"] == {"scope": "fgt", "fmg_user": "admin"}
p = f["topology"]["pve"]
assert p["server_name"] == "pve01" and "server_id" not in p and "url" not in p and "token_id" not in p, p
assert "SECRET-X" not in r.text and "leak" not in r.text
print("OK  export: hele topologie als JSON, server op naam, geen id/url/token/secret")

# ---- import: the same topology back, server id from the name of this tool's server
r = c.post("/api/settings/import", json=f)
assert r.status_code == 200, r.text
back = r.json()
bt = back["topology"]
assert bt["pve"]["server_id"] == 7 and bt["pve"]["first_vmid"] == 500 and back["ui"]["scope"] == "fgt" and back["notes"] == []
for k in ("adom", "hubs", "edges", "region", "fmgr", "wanemu"):
    assert bt[k] == model.merged(t)[k], k
assert bt["region"]["multi_vrf"] is True and len(bt["edges"]) == 2 and bt["hubs"][0]["serial"] == "FGVMMLTM26000001"
# unknown server: imported without it, with a note
g = json.loads(json.dumps(f)); g["topology"]["pve"]["server_name"] = "elsewhere"
r = c.post("/api/settings/import", json=g).json()
assert r["topology"]["pve"]["server_id"] is None and any("elsewhere" in n for n in r["notes"])
# fixed values cannot be changed through a file
g = json.loads(json.dumps(f)); g["topology"]["wanemu"]["template"] = "local:vztmpl/evil.tar.zst"; g["topology"]["platform"] = "X"
r = c.post("/api/settings/import", json=g).json()["topology"]
assert r["wanemu"]["template"] == model.DEFAULTS["wanemu"]["template"] and r["platform"] == "FortiGate-VM64"
# not a settings file
for bad in ({"format": "other"}, {"format": "fgt-sdwan-settings", "version": 99, "topology": {}}, {"format": "fgt-sdwan-settings", "version": 1}):
    assert c.post("/api/settings/import", json=bad).status_code == 422, bad
print("OK  import: zelfde topologie terug, server-id via naam (onbekend -> melding), vaste waarden niet te wijzigen, fout bestand -> 422")

# ---- saved settings in the tool: description required, list, load, delete
assert c.post("/api/settings/saved", json={"description": " ", "topology": t}).status_code == 422
r = c.post("/api/settings/saved", json={"description": "Lab SD-WAN 2 hubs", "topology": t, "ui": {"scope": "all"}})
assert r.status_code == 200, r.text
sid = r.json()["id"]
c.post("/api/settings/saved", json={"description": "second", "topology": model.merged({})})
lst = c.get("/api/settings/saved").json()
assert [x["description"] for x in lst] == ["second", "Lab SD-WAN 2 hubs"], lst              # newest first
x = next(x for x in lst if x["id"] == sid)
assert x["adom"] == "LAB-1" and x["devices"] == 4 and x["created"] and "topology" not in x
row = conn.execute("SELECT data FROM saved_settings WHERE id=?", (sid,)).fetchone()["data"]
assert "SECRET-X" not in row and "server_id" not in row and '"server_name": "pve01"' in row
got = c.get(f"/api/settings/saved/{sid}").json()
assert got["description"] == "Lab SD-WAN 2 hubs" and got["topology"]["pve"]["server_id"] == 7 and got["ui"]["scope"] == "all"
assert got["topology"]["wanemu"] == bt["wanemu"]
# update in place (same id) with a new description
r = c.put(f"/api/settings/saved/{sid}", json={"description": "Lab v2", "topology": t})
assert r.status_code == 200 and c.get(f"/api/settings/saved/{sid}").json()["description"] == "Lab v2"
assert c.delete(f"/api/settings/saved/{sid}").status_code == 200
assert c.get(f"/api/settings/saved/{sid}").status_code == 404 and len(c.get("/api/settings/saved").json()) == 1
assert c.put("/api/settings/saved/9999", json={"description": "x", "topology": t}).status_code == 404
print("OK  opgeslagen instellingen: beschrijving verplicht, lijst (nieuwste eerst), laden, bijwerken, verwijderen; geen secrets")

# the export module does not depend on the API
assert settings_io.FORMAT == "fgt-sdwan-settings"
print("alle settings-tests geslaagd")
