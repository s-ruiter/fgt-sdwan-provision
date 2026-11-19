"""FortiFlex in the tool: stored credentials (password never returned), configs, tool licenses, stop, check, deploy."""
import json
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import HTTPException  # noqa: E402

from app import fortiflex as ff, main, model  # noqa: E402
from tests import fake_fortiflex as fake  # noqa: E402

srv = fake.start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
ff.AUTH_URL, ff.BASE = URL + "/api/v1/oauth/token/", URL + "/ES/api/fortiflex/v2"


def err(fn, *a):
    try:
        fn(*a)
    except HTTPException as e:
        return e.status_code, str(e.detail)
    raise SystemExit(f"{fn.__name__} gaf geen fout")


assert main.fortiflex_get() == {"user": "", "has_password": False, "enabled": False, "program": "", "fmg_config": None,
                                "fgt_config": None}
code, msg = err(main.fortiflex_test, main.FlexCreds(user=fake.USER, password="fout"))
assert code == 400 and "geweigerd" in msg
r = main.fortiflex_test(main.FlexCreds(user=fake.USER, password=fake.PASSWORD))
assert r["programs"][0]["serialNumber"] == "ELAVMS0000001234"
main.fortiflex_put(main.FlexSettings(user=fake.USER, password=fake.PASSWORD, enabled=True, program="ELAVMS0000001234",
                                     fmg_config=20, fgt_config=10))
g = main.fortiflex_get()
assert g["has_password"] and g["enabled"] and g["fgt_config"] == 10 and fake.PASSWORD not in json.dumps(g)
main.fortiflex_put(main.FlexSettings(user=fake.USER, password="", enabled=True, program="ELAVMS0000001234",
                                     fmg_config=20, fgt_config=10))          # empty password = keep the stored one
assert main.fortiflex_test(main.FlexCreds())["programs"]
cf = main.fortiflex_configs("ELAVMS0000001234")
assert [(c["id"], c["product"]) for c in cf] == [(10, "FortiGate VM"), (11, "FortiGate VM"), (20, "FortiManager VM")]
lic = main.fortiflex_entitlements()["entitlements"]
assert {e["serialNumber"] for e in lic} == {"FGVMELTM26000001", "FGVMELTM26000003", "FGVMELTM26000004", "FMGVMSTM26000001"}
assert all("token" not in e for e in lic)                                       # tokens never to the browser
code, _ = err(main.fortiflex_stop, "FGVMELTM26000009")                          # not a tool license
assert code == 404 and fake.STATE["ents"]["FGVMELTM26000009"]["status"] == "ACTIVE"
main.fortiflex_stop("FGVMELTM26000001")
assert fake.STATE["ents"]["FGVMELTM26000001"]["status"] == "STOPPED"
print("OK  FortiFlex-instellingen: wachtwoord bewaard en nooit terug, testen, configs, licenties van de tool (zonder tokens), stop")

# check: lookup only, per device serial + action; nothing created
fake.reset()
topo = model.merged({"adom": "SDWAN"})
req = lambda scope="all", dry=True, **b: main.DeployReq(topology=topo, fmg=main.Fmgcfg(password="x"), dry_run=dry,
                                                       scope=scope, bootstrap=dict({"fmg": "day0", "fgt": "day0"}, **b))
n = len(fake.STATE["calls"])
c = main.fortiflex_check(req())
assert {x["name"]: x["action"] for x in c["devices"]} == {"fmg": "use", "site1-H1": "regen", "site1-H2": "create",
                                                         "site1-1": "reactivate", "site1-2": "create", "site1-3": "create"}
assert all(p.endswith("/list") or "oauth" in p for p, _ in fake.STATE["calls"][n:])
assert [x["name"] for x in main.fortiflex_check(req("fgt"))["devices"]][0] == "site1-H1"   # existing FMG: no FMG license
assert main.fortiflex_check(req("dev"))["devices"] == [] and main.fortiflex_check(req("all", fgt="none"))["devices"][0]["name"] == "fmg"
print("OK  controleren: alleen opzoeken; FortiManager alleen als hij een token nodig heeft; niets bij model devices/templates")

# deploy helper: fills serials + tokens (real run), placeholders (dry run), nothing when disabled
r = req(dry=False)
t2 = model.merged({"adom": "SDWAN"})
log = main._licenses(t2, r)
assert t2["hubs"][0]["serial"] == "FGVMELTM26000001" and r.secrets.fgt_tokens["site1-H1"].startswith("RGN")
assert r.secrets.fmg_token == "TOK000001AAAAAAAAAAA" and t2["edges"][1]["serial"].startswith("FGVMELTM2699")
assert any("site1-2" in m and "nieuwe licentie" in m for _, m in log) and not any("RGN" in m or "TOK" in m for _, m in log)
rd = req(dry=True)
t3 = model.merged({"adom": "SDWAN"})
main._licenses(t3, rd)
assert t3["edges"][2]["serial"].startswith("FGVMELTM2699")                      # created in the real run above
fake.reset()                                                                   # dry run: several new -> unique placeholders
t5 = model.merged({"adom": "SDWAN"})
main._licenses(t5, req(dry=True))
sns = [d["serial"] for d in t5["hubs"] + t5["edges"]]
assert len(set(sns)) == len(sns) and "FGVMNEWLIC000001" in sns and not any("serienummer" in e for e in model.validate(t5)), (sns, model.validate(t5))
main.fortiflex_put(main.FlexSettings(user=fake.USER, password="", enabled=False, program="ELAVMS0000001234", fmg_config=20, fgt_config=10))
t4 = model.merged({"adom": "SDWAN"})
assert main._licenses(t4, req(dry=False)) == [] and t4["hubs"][0]["serial"] == ""
srv.shutdown()
print("OK  uitrol: serienummers in de topologie en tokens in de job-geheimen; dry-run zonder aanmaken; uit = niets")
