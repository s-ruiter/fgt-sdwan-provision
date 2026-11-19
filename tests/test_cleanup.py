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
conn = db.connect(os.environ["DATA_DIR"])
sid = conn.execute("INSERT INTO servers(name,url,token_id,token_secret,verify_tls) VALUES('pve','http://127.0.0.1:8087','root@pam!t','secret',0)").lastrowid
conn.commit()
db.put_secret(conn, "wanemu_addr", "10.10.106.250:8090")
db.put_secret(conn, "wanemu_forwards", json.dumps({"host": "x", "forwards": []}))
r = main.cleanup_plan(main.CleanupReq(server_id=sid, node="pve1"))
assert [i["name"] for i in r["items"]] == ["fmg"]
try:
    main.cleanup_run(main.CleanupReq(server_id=sid, node="pve1", confirm="delete"))
    raise SystemExit("zonder 'DELETE' hoort het te weigeren")
except HTTPException as e:
    assert e.status_code == 422 and "DELETE" in str(e.detail)
assert "108" in S["vms"]
r = main.cleanup_run(main.CleanupReq(server_id=sid, node="pve1", confirm="DELETE"))
assert r["results"][0]["ok"] and not S["vms"]
assert db.get_secret(conn, "wanemu_addr") in (None, "") and db.get_secret(conn, "wanemu_forwards") in (None, "")
srv.shutdown()
print("OK  API: plan via opgeslagen server, verwijderen alleen met 'DELETE', WAN-emulatie-adres in de tool gewist")
