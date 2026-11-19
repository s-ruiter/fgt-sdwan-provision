"""FortiFlex: client against a fake API, the license plan per device, resolve (dry and real)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fortiflex as ff  # noqa: E402
from tests import fake_fortiflex as fake  # noqa: E402

srv = fake.start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
cl = lambda pw=fake.PASSWORD: ff.Client(fake.USER, pw, auth_url=URL + "/api/v1/oauth/token/", base=URL + "/ES/api/fortiflex/v2")

try:
    cl("fout").programs()
    raise SystemExit("verwachtte loginfout")
except ff.FlexError as e:
    assert "login" in str(e).lower() and "fout" not in str(e)          # password never in the message
c = cl()
assert c.programs()[0]["serialNumber"] == "ELAVMS0000001234"
assert [x["id"] for x in c.configs("ELAVMS0000001234")] == [10, 11, 20]
assert {x["serialNumber"] for x in c.entitlements(10)} == {"FGVMELTM26000001", "FGVMELTM26000002", "FGVMELTM26000003", "FGVMELTM26000004"}
assert ff.desc("SDWAN", "site1-H1") == "fgt-sdwan SDWAN site1-H1"
print("OK  client: login (fout wachtwoord niet in melding), programma's, configs, entitlements")

# plan
devs = [{"name": "fmg", "serial": "", "config_id": 20},
        {"name": "site1-H1", "serial": "", "config_id": 10},                    # description match, token USED -> regen
        {"name": "site1-H2", "serial": "FGVMELTM26000002", "config_id": 10},    # serial match, other descr -> use + rename
        {"name": "site1-1", "serial": "", "config_id": 10},                     # STOPPED -> reactivate
        {"name": "site1-2", "serial": "", "config_id": 10},                     # EXPIRED -> create
        {"name": "site1-3", "serial": "FGVMELTM26000009", "config_id": 10}]     # serial in other config -> create
ents = {10: c.entitlements(10), 20: c.entitlements(20)}
p = {r["name"]: r for r in ff.plan(devs, ents, "SDWAN")}
assert (p["fmg"]["action"], p["fmg"]["serial"]) == ("use", "FMGVMSTM26000001")
assert (p["site1-H1"]["action"], p["site1-H1"]["serial"]) == ("regen", "FGVMELTM26000001")
assert (p["site1-H2"]["action"], p["site1-H2"]["serial"], p["site1-H2"]["rename"]) == ("use", "FGVMELTM26000002", True)
assert (p["site1-1"]["action"], p["site1-1"]["serial"]) == ("reactivate", "FGVMELTM26000003")
assert (p["site1-2"]["action"], p["site1-2"]["serial"]) == ("create", None)
assert (p["site1-3"]["action"], p["site1-3"]["serial"]) == ("create", None)
dup = ff.plan([{"name": "a", "serial": "FGVMELTM26000002", "config_id": 10},
               {"name": "b", "serial": "FGVMELTM26000002", "config_id": 10}], ents, "SDWAN")
assert [r["action"] for r in dup] == ["use", "create"]                          # one entitlement per device
print("OK  plan: gebruiken / nieuw token / heractiveren / aanmaken; serienummer vóór omschrijving; andere config niet; nooit dubbel")

# dry: nothing changes
n = len(fake.STATE["calls"])
res = ff.resolve(c, list(p.values()), dry=True)
assert all(path.endswith("/list") or "oauth" in path for path, _ in fake.STATE["calls"][n:]), fake.STATE["calls"][n:]
assert res["site1-2"]["serial"] == "FGVMNEWLIC000001" and res["site1-3"]["serial"] == "FGVMNEWLIC000002" and res["fmg"]["serial"] == "FMGVMSTM26000001" and res["site1-2"]["token"] is None
assert res["site1-H1"]["token"] is None                                         # dry: no regenerated token
# real
res = ff.resolve(c, list(p.values()), dry=False)
assert res["fmg"]["token"] == "TOK000001AAAAAAAAAAA"
assert res["site1-H1"]["token"].startswith("RGN") and fake.STATE["ents"]["FGVMELTM26000001"]["tokenStatus"] == "NOTUSED"
assert fake.STATE["ents"]["FGVMELTM26000002"]["description"] == "fgt-sdwan SDWAN site1-H2"
assert fake.STATE["ents"]["FGVMELTM26000003"]["status"] == "ACTIVE" and res["site1-1"]["token"].startswith("RGN")
assert res["site1-2"]["serial"].startswith("FGVMELTM2699") and res["site1-2"]["token"].startswith("NEW")
assert fake.STATE["ents"][res["site1-2"]["serial"]]["description"] == "fgt-sdwan SDWAN site1-2"
create = [b for path, b in fake.STATE["calls"] if path.endswith("/vm/create")]
assert create and all(b["count"] == 1 and b["endDate"] is None for b in create)
print("OK  resolve: dry roept alleen /list aan; echt: token / nieuw token / heractiveren+token / aanmaken / omschrijving")

# failure halfway: message names what was already created
fake.reset()
fake.STATE["fail"] = "/vm/token"
fake.STATE["fail"] = None
p = ff.plan([{"name": "x", "serial": "", "config_id": 10}, {"name": "site1-1", "serial": "", "config_id": 10},
             {"name": "site1-H1", "serial": "", "config_id": 10}], {10: c.entitlements(10)}, "SDWAN")
fake.STATE["fail"] = "/vm/token"
try:
    ff.resolve(c, p, dry=False)
    raise SystemExit("verwachtte fout")
except ff.FlexError as e:
    m = str(e)
    assert "not enough points" in m and "x" in m and "created" in m and "site1-1" in m and "reactivated" in m, m
srv.shutdown()
print("OK  fout halverwege: melding van FortiFlex plus wat al aangemaakt is")

# asset folder: only on create, as folderPath; empty = FortiFlex default
srv = fake.start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
c = ff.Client(fake.USER, fake.PASSWORD, auth_url=URL + "/api/v1/oauth/token/", base=URL + "/ES/api/fortiflex/v2")
fake.reset()
rows = ff.plan([{"name": "n1", "serial": "", "config_id": 10}, {"name": "n2", "serial": "", "config_id": 10}],
               {10: c.entitlements(10)}, "SDWAN")
res = ff.resolve(c, rows[:1], dry=False, folder="My Assets/Lab/SD-WAN")
assert fake.STATE["folders"][res["n1"]["serial"]] == "My Assets/Lab/SD-WAN"
res = ff.resolve(c, rows[1:], dry=False)
assert [b for p_, b in fake.STATE["calls"] if p_.endswith("/vm/create")][-1].get("folderPath") is None
assert ff.check_folder("") == "" and ff.check_folder(" My Assets/Lab/ ") == "My Assets/Lab"
for bad in ("Lab/SD-WAN", "My Assets//x", "My Assets/" + "x" * 300):
    try:
        ff.check_folder(bad)
        raise SystemExit(f"accepteerde {bad}")
    except ValueError:
        pass
srv.shutdown()
print("OK  asset folder: folderPath alleen bij aanmaken, leeg = standaard; pad moet onder 'My Assets' liggen")

# asset folders via the Asset Management API v3 (separate OAuth client 'assetmanagement')
srv = fake.start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
ff.ASSET_BASE = URL + "/ES/api/registration/v3"
c = ff.Client(fake.USER, fake.PASSWORD, auth_url=URL + "/api/v1/oauth/token/", base=URL + "/ES/api/fortiflex/v2")
fake.reset()
assert c.programs()                                          # flexvm token first, then the folder list gets its own
f = c.asset_folders(account_id=1)
assert f == [{"id": 1, "path": "My Assets/Lab"}, {"id": 2, "path": "My Assets/Lab/SD-WAN"}], f
assert fake.STATE["folder_list_bodies"][-1] == {"accountId": 1}
logins = [b["client_id"] for p_, b in fake.STATE["calls"] if "oauth" in p_]
assert logins == ["flexvm", "assetmanagement"], logins
assert all(ff.check_folder(x["path"]) == x["path"] for x in f)
srv.shutdown()
print("OK  asset folders: eigen login (assetmanagement), accountId mee, paden als 'My Assets/...'")
