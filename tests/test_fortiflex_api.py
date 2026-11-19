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
cf = main.fortiflex_configs(main.FlexConfigsReq(program="ELAVMS0000001234"))
assert main.fortiflex_configs(main.FlexConfigsReq(program="ELAVMS0000001234", user=fake.USER, password=fake.PASSWORD)) == cf
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
log = main._licenses(t2, r, dry=False)
assert t2["hubs"][0]["serial"] == "FGVMELTM26000001" and r.secrets.fgt_tokens["site1-H1"].startswith("RGN")
assert r.secrets.fmg_token == "TOK000001AAAAAAAAAAA" and t2["edges"][1]["serial"].startswith("FGVMELTM2699")
assert any("site1-2" in m and "nieuwe licentie" in m for _, m in log) and not any("RGN" in m or "TOK" in m for _, m in log)
rd = req(dry=True)
t3 = model.merged({"adom": "SDWAN"})
main._licenses(t3, rd, dry=True)
assert t3["edges"][2]["serial"].startswith("FGVMELTM2699")                      # created in the real run above
fake.reset()                                                                   # dry run: several new -> unique placeholders
t5 = model.merged({"adom": "SDWAN"})
main._licenses(t5, req(dry=True), dry=True)
sns = [d["serial"] for d in t5["hubs"] + t5["edges"]]
assert len(set(sns)) == len(sns) and "FGVMNEWLIC000001" in sns and not any("serienummer" in e for e in model.validate(t5)), (sns, model.validate(t5))
main.fortiflex_put(main.FlexSettings(user=fake.USER, password="", enabled=False, program="ELAVMS0000001234", fmg_config=20, fgt_config=10))
t4 = model.merged({"adom": "SDWAN"})
assert main._licenses(t4, req(dry=False), dry=False) == [] and t4["hubs"][0]["serial"] == ""
srv.shutdown()
print("OK  uitrol: serienummers in de topologie en tokens in de job-geheimen; dry-run zonder aanmaken; uit = niets")

# ---- review fixes
import threading as _th  # noqa: E402
srv = fake.start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
ff.AUTH_URL, ff.BASE = URL + "/api/v1/oauth/token/", URL + "/ES/api/fortiflex/v2"
fake.reset()
main.fortiflex_put(main.FlexSettings(user=fake.USER, password="", enabled=True, program="ELAVMS0000001234", fmg_config=20, fgt_config=10))
st = main.fortiflex_stop("FGVMELTM26000003")                                       # tool license (descr fgt-sdwan ...)
assert "token" not in json.dumps(st) and st["status"] == "STOPPED", st
fake.reset()


def deploy_req(dry, **topo_over):
    t = model.merged(dict({"adom": "SDWAN"}, **topo_over))
    t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["initial_ip"] = f"192.168.1.{10 + i}"
    return main.DeployReq(topology=t, fmg=main.Fmgcfg(password="x"), dry_run=dry, scope="all",
                          bootstrap={"fmg": "ssh", "fgt": "ssh"}, secrets=main.Secrets(fgt_new_pw="Pw-123456"))


changing = lambda: [p for p, _ in fake.STATE["calls"] if not (p.endswith("/list") or "oauth" in p)]
# 1. dry run with FortiFlex on: accepted (placeholder tokens), nothing created
j = main.deploy(deploy_req(True))
job = main.JOBS[j["job"]]
assert job["steps"]["licenses"] == "ok" and changing() == [], changing()
# 2. invalid request: refused BEFORE any license is created / token regenerated
bad = deploy_req(False)
bad.topology["fmgr"]["gateway"] = ""
code, msg = err(main.deploy, bad)
assert code == 422 and changing() == [], changing()
# real run: licenses resolved, serials in the job's topology
j = main.deploy(deploy_req(False))
assert any(p.endswith("/vm/create") for p in changing()) and main.JOBS[j["job"]]["steps"]["licenses"] == "ok"
# 3. concurrent: the real resolution waits for the lock
fake.reset()
n0 = len(changing())
main._FLEX_LOCK.acquire()
th = _th.Thread(target=lambda: main.deploy(deploy_req(False)))
th.start()
th.join(1.0)
assert th.is_alive() and len(changing()) == n0                                    # blocked, nothing changed yet
main._FLEX_LOCK.release()
th.join(10)
assert not th.is_alive() and len(changing()) > n0
srv.shutdown()
print("OK  review: dry-run met FortiFlex werkt, ongeldige uitrol maakt niets aan, licenties onder een lock, stop zonder token")
