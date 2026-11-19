"""Deployment jobs survive a refresh and a service restart.  python -m tests.test_jobs"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
from app import db, main

c = db.connect(tmp)
steps = {"pve_fmg": "ok", "bootstrap_fmg": "running", "preflight": "pending"}
job = {"id": "aaaa1111", "created": "2026-11-17 10:00:00", "title": "Uitrol van X", "state": "running", "dry_run": False,
       "steps": dict(steps), "events": [{"t": "10:00:01", "step": "pve_fmg", "status": "ok", "msg": "klaar"}]}
db.save_job(c, job)
job["events"].append({"t": "10:00:05", "step": "bootstrap_fmg", "status": "running", "msg": "wachten"})
db.save_job(c, job)                                              # update, not duplicate
assert len(db.list_jobs(c)) == 1 and db.load_job(c, "aaaa1111")["events"][-1]["msg"] == "wachten"
done = {"id": "bbbb2222", "created": "2026-11-17 09:00:00", "title": "Uitrol van Y", "state": "done", "dry_run": True,
        "steps": {"pve_fmg": "ok"}, "events": []}
db.save_job(c, done)
assert [j["id"] for j in db.list_jobs(c)] == ["aaaa1111", "bbbb2222"]          # newest first

# a restart: the 'running' job is marked interrupted (never left 'running'), finished ones are untouched
c2 = db.connect(tmp)
assert db.interrupt_running_jobs(c2, "10:05:00") == 1
j = db.load_job(c2, "aaaa1111")
assert j["state"] == "failed" and j["steps"]["bootstrap_fmg"] == "error" and "restarted" in j["events"][-1]["msg"], j
assert j["events"][-1]["status"] == "error" and len(j["events"]) == 3                   # history kept + one explanation
assert db.load_job(c2, "bbbb2222")["state"] == "done"
assert db.interrupt_running_jobs(c2, "10:06:00") == 0                                    # idempotent

# the app serves jobs from the database when they are not in memory (after a restart)
main.JOBS.clear()
got = main._get_job("aaaa1111")
assert got["title"] == "Uitrol van X" and got["state"] == "failed"
assert main._get_job("nope") is None
assert main.jobs_list()["latest"] == "aaaa1111"
try:
    main.job("nope")
    raise SystemExit("geen 404")
except main.HTTPException as e:
    assert e.status_code == 404 and "does not exist" in e.detail
# a live job wins over its (older) stored row
main.JOBS["cccc3333"] = {"id": "cccc3333", "state": "running", "events": [], "steps": {}, "created": "2026-11-17 11:00:00", "title": "t", "dry_run": False}
db.save_job(c2, dict(main.JOBS["cccc3333"], state="running"))
assert main.jobs_list()["latest"] == "cccc3333" and main.jobs_list()["jobs"][0]["state"] == "running"
mode = oct(os.stat(os.path.join(tmp, "fgt-sdwan.db")).st_mode & 0o777)
assert mode == "0o600", mode
print("OK  jobs: bewaard in de database, overleven een herstart (als onderbroken gemarkeerd), voortgang opvraagbaar na refresh, db 0600")
