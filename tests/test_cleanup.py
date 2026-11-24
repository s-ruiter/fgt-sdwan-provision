"""Cleanup: remove everything the tool deployed on Proxmox (tagged fgt-sdwan), never the deploy container itself."""
import json
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import HTTPException  # noqa: E402

from app import cleanup, db, main  # noqa: E402
from app.pve import Pve  # noqa: E402
from tests import fake_proxmox as fp  # noqa: E402

fp.reset()
srv = fp.start(8089)
pve = Pve("http://127.0.0.1:8089", "root@pam!t", "secret", poll=0)
S = fp.STATE
for vmid, name, tags, st in ((108, "fmg", "fgt-sdwan", "running"), (112, "site1-H1", "fgt-sdwan", "running"),
                             (113, "site1-H2", "fgt-sdwan", "stopped"), (200, "my-own-vm", "", "running")):
    S["vms"][str(vmid)] = {"vmid": vmid, "name": name, "params": {"tags": tags}, "status": st}
for vmid, host, tags in ((100, "fgt-sdwan", ""), (101, "fgt-sdwan", "fgt-sdwan"),     # deploy CT: never, even if tagged
                         (111, "wan-emu", "fgt-sdwan"), (117, "client-site1-1", "fgt-sdwan"), (150, "other-ct", "x")):
    S["cts"][str(vmid)] = {"vmid": vmid, "params": {"hostname": host, "tags": tags}, "status": "running"}
S["uploads"].update({"local:iso/fgt-sdwan-site1-1-3e050117.iso": b"x", "local:iso/fgt-sdwan-fmg-5427250b.iso": b"x",
                     "local:iso/debian.iso": b"x", "local:import/fgt-sdwan-ab12cd34-fgt.qcow2": b"x"})

p = cleanup.plan(pve, "pve1")
got = sorted((i["kind"], i["name"]) for i in p)
assert got == [("ct", "client-site1-1"), ("ct", "wan-emu"),
               ("file", "local:import/fgt-sdwan-ab12cd34-fgt.qcow2"), ("file", "local:iso/fgt-sdwan-fmg-5427250b.iso"),
               ("file", "local:iso/fgt-sdwan-site1-1-3e050117.iso"),
               ("vm", "fmg"), ("vm", "site1-H1"), ("vm", "site1-H2")], got
print("OK  plan: alleen getagde VM's/CT's en fgt-sdwan-bestanden; deploy-CT 'fgt-sdwan' nooit, ook niet getagd")

log = []
res = cleanup.run(pve, "pve1", p, lambda m: log.append(m))
assert all(r["ok"] for r in res), res
assert set(S["vms"]) == {"200"} and set(S["cts"]) == {"100", "101", "150"}, (S["vms"].keys(), S["cts"].keys())
assert set(S["uploads"]) == {"local:iso/debian.iso"}, S["uploads"].keys()
assert ("POST", "/nodes/pve1/qemu/108/status/stop") in S["calls"] and ("POST", "/nodes/pve1/qemu/113/status/stop") not in S["calls"]
assert cleanup.plan(pve, "pve1") == []
print("OK  run: draaiende VM's/CT's eerst gestopt, alles verwijderd met schijven, eigen VM/CT en andere ISO's blijven")

# a failure on one item does not stop the rest
S["vms"]["120"] = {"vmid": 120, "name": "site1-1", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}
S["cts"]["121"] = {"vmid": 121, "params": {"hostname": "client-site1-2", "tags": "fgt-sdwan"}, "status": "stopped"}
S["fail_delete"] = "120"
res = cleanup.run(pve, "pve1", cleanup.plan(pve, "pve1"), lambda m: None)
assert [(r["name"], r["ok"]) for r in res] == [("site1-1", False), ("client-site1-2", True)], res
S["fail_delete"] = None
srv.shutdown()
print("OK  fout bij één onderdeel: de rest gaat door, resultaat per onderdeel")

# API: confirmation required, plan via the saved server
fp.reset()
srv = fp.start(8087)
S = fp.STATE
S["vms"]["108"] = {"vmid": 108, "name": "fmg", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}
S["cts"]["111"] = {"vmid": 111, "params": {"hostname": "lab1-wan-emu", "tags": "fgt-sdwan"}, "status": "stopped"}   # name prefix
S["cts"]["112"] = {"vmid": 112, "params": {"hostname": "lab1-trafficgen", "tags": "fgt-sdwan"}, "status": "stopped"}
S["cts"]["113"] = {"vmid": 113, "params": {"hostname": "lab1-client-site1-1", "tags": "fgt-sdwan"}, "status": "stopped"}
conn = db.connect(os.environ["DATA_DIR"])
sid = conn.execute("INSERT INTO servers(name,url,token_id,token_secret,verify_tls) VALUES('pve','http://127.0.0.1:8087','root@pam!t','secret',0)").lastrowid
conn.commit()
db.put_secret(conn, "wanemu_addr", "10.10.106.250:8090")
db.put_secret(conn, "wanemu_forwards", json.dumps({"host": "x", "forwards": []}))
db.put_secret(conn, "access_creds", json.dumps({"fmg": {"name": "fmg", "host": "10.10.106.11", "user": "admin", "password": "x"},
                                                "fgt": {"user": "admin", "password": "y"},
                                                "client": {"user": "root", "password": "z", "list": []}}))
db.put_secret(conn, "trafficgen_addr", "10.10.106.249:8443")
db.put_secret(conn, "trafficgen_password", "Tg-Admin-pw1!")
r = main.cleanup_plan(main.CleanupReq(server_id=sid, node="pve1"))
assert [i["name"] for i in r["items"]] == ["fmg", "lab1-wan-emu", "lab1-trafficgen", "lab1-client-site1-1"], r["items"]
try:
    main.cleanup_run(main.CleanupReq(server_id=sid, node="pve1", confirm="delete"))
    raise SystemExit("zonder 'DELETE' hoort het te weigeren")
except HTTPException as e:
    assert e.status_code == 422 and "DELETE" in str(e.detail)
assert "108" in S["vms"]
r = main.cleanup_run(main.CleanupReq(server_id=sid, node="pve1", confirm="DELETE"))
assert all(x["ok"] for x in r["results"]) and not S["vms"] and not S["cts"]
assert db.get_secret(conn, "wanemu_addr") in (None, "") and db.get_secret(conn, "wanemu_forwards") in (None, "")
assert db.get_secret(conn, "trafficgen_addr") in (None, "") and db.get_secret(conn, "trafficgen_password") in (None, "")
assert db.get_secret(conn, "access_creds") in (None, "")                     # FMG + wan-emu + clients gone: logins too
srv.shutdown()
print("OK  API: plan via opgeslagen server, verwijderen alleen met 'DELETE', WAN-emulatie-adres in de tool gewist")

# ---- review fixes
import threading  # noqa: E402
from app import fortiflex as ff  # noqa: E402
from tests import fake_fortiflex as fx  # noqa: E402
fp.reset()
srv = fp.start(8085)
pve = Pve("http://127.0.0.1:8085", "root@pam!t", "secret", poll=0)
S = fp.STATE
S["shared_iso"] = True
S["vms"]["108"] = {"vmid": 108, "name": "fmg", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}
S["vms"]["112"] = {"vmid": 112, "name": "site1-H1", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}
S["cts"]["111"] = {"vmid": 111, "params": {"hostname": "wan-emu", "tags": "fgt-sdwan"}, "status": "stopped"}
S["uploads"].update({"local:iso/fgt-sdwan-site1-1-3e050117.iso": b"x",    # tool Day-0 ISO: yes
                     "local:iso/fgt-sdwan-7.4.iso": b"x",                 # user's own upload: no
                     "nfs:iso/fgt-sdwan-site9-aaaaaaaa.iso": b"x",        # shared storage (other node's VM?): no
                     "local:import/fgt-sdwan-ab12cd34-fgt.qcow2": b"x"})  # tool import: yes
names = sorted(i["name"] for i in cleanup.plan(pve, "pve1") if i["kind"] == "file")
assert names == ["local:import/fgt-sdwan-ab12cd34-fgt.qcow2", "local:iso/fgt-sdwan-site1-1-3e050117.iso"], names
print("OK  bestanden: alleen het naampatroon van de tool en niet op gedeelde opslag")

# only what was shown: a VM tagged after the overview is not deleted
p = cleanup.plan(pve, "pve1")
S["vms"]["130"] = {"vmid": 130, "name": "late-vm", "params": {"tags": "fgt-sdwan"}, "status": "running"}
res = cleanup.run(pve, "pve1", cleanup.plan(pve, "pve1"), lambda m: None, only={i["name"] for i in p})
assert "130" in S["vms"] and not any(r["name"] == "late-vm" for r in res)
# status checked again right before deleting: a VM started after the plan is stopped first
S["vms"]["131"] = {"vmid": 131, "name": "site1-2", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}
p2 = [i for i in cleanup.plan(pve, "pve1") if i["name"] == "site1-2"]
S["vms"]["131"]["status"] = "running"
cleanup.run(pve, "pve1", p2, lambda m: None)
assert ("POST", "/nodes/pve1/qemu/131/status/stop") in S["calls"] and "131" not in S["vms"]
print("OK  alleen wat in het overzicht stond; status vlak voor verwijderen opnieuw gecontroleerd")

# API: licences limited to the deleted devices; wan-emu address kept when wan-emu was not deleted; lock vs deploy
fs = fx.start()
U = f"http://127.0.0.1:{fs.server_address[1]}"
ff.AUTH_URL, ff.BASE = U + "/api/v1/oauth/token/", U + "/ES/api/fortiflex/v2"
fx.reset()
fx.STATE["ents"]["FGVMELTM26000077"] = {"serialNumber": "FGVMELTM26000077", "configId": 10, "description": "fgt-sdwan OTHER site9",
                                         "status": "ACTIVE", "tokenStatus": "USED", "token": "T" * 20, "accountId": 1, "endDate": "x"}
main.fortiflex_put(main.FlexSettings(user=fx.USER, password=fx.PASSWORD, enabled=True, program="ELAVMS0000001234",
                                     fmg_config=20, fgt_config=10))
sid2 = conn.execute("INSERT INTO servers(name,url,token_id,token_secret,verify_tls) VALUES('pve2','http://127.0.0.1:8085','root@pam!t','secret',0)").lastrowid
conn.commit()
S["vms"]["108"] = {"vmid": 108, "name": "fmg", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}   # again (deleted above)
S["vms"]["112"] = {"vmid": 112, "name": "site1-H1", "params": {"tags": "fgt-sdwan"}, "status": "stopped"}
S["cts"]["111"] = {"vmid": 111, "params": {"hostname": "wan-emu", "tags": "fgt-sdwan"}, "status": "stopped"}
S["vms"].pop("130", None)
S["fail_delete_ct"] = "111"
db.put_secret(conn, "wanemu_addr", "10.10.106.250:8090")
pl = main.cleanup_plan(main.CleanupReq(server_id=sid2, node="pve1", stop_licences=True))
assert sorted(x["serialNumber"] for x in pl["licences"]) == ["FGVMELTM26000001", "FMGVMSTM26000001"], pl["licences"]  # H1 + fmg only
r = main.cleanup_run(main.CleanupReq(server_id=sid2, node="pve1", confirm="DELETE", stop_licences=True,
                                     only=[i["name"] for i in pl["items"]]))
assert fx.STATE["ents"]["FGVMELTM26000001"]["status"] == "STOPPED" and fx.STATE["ents"]["FGVMELTM26000077"]["status"] == "ACTIVE"
assert db.get_secret(conn, "wanemu_addr") == "10.10.106.250:8090"                 # wan-emu delete failed: keep its address
S["fail_delete_ct"] = None
main._CLEANUP_LOCK.acquire()
try:
    try:
        main.cleanup_run(main.CleanupReq(server_id=sid2, node="pve1", confirm="DELETE"))
        raise SystemExit("tweede opruiming tegelijk hoort te weigeren")
    except HTTPException as e:
        assert e.status_code == 409
    try:
        main.deploy(main.DeployReq(topology={}, fmg=main.Fmgcfg(password="x"), dry_run=True))
        raise SystemExit("uitrol tijdens opruimen hoort te weigeren")
    except HTTPException as e:
        assert e.status_code == 409 and "cleanup" in str(e.detail).lower()
finally:
    main._CLEANUP_LOCK.release()
srv.shutdown(); fs.shutdown()
print("OK  API: alleen licenties van verwijderde devices, wan-emu-adres blijft als wan-emu bleef, opruimen en uitrollen sluiten elkaar uit")
