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
    assert "inloggen" in str(e).lower() and "fout" not in str(e)          # password never in the message
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
    assert "not enough points" in m and "x" in m and "aangemaakt" in m and "site1-1" in m and "heractiveerd" in m, m
srv.shutdown()
print("OK  fout halverwege: melding van FortiFlex plus wat al aangemaakt is")
