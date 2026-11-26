"""Cancel a running deployment from the progress page.  python -m tests.test_cancel"""
import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from concurrent.futures import ThreadPoolExecutor
from app import cancel, main

# 1. cancel.sleep: a normal sleep without a job; with a job it wakes up at once on cancel and raises Cancelled
t0 = time.monotonic(); cancel.sleep(0.05); assert time.monotonic() - t0 >= 0.04
ev = threading.Event()
cancel.bind(ev)
threading.Timer(0.1, ev.set).start()
t0 = time.monotonic()
try:
    cancel.sleep(30)
    raise SystemExit("not cancelled")
except cancel.Cancelled:
    assert time.monotonic() - t0 < 2
assert not isinstance(cancel.Cancelled(), Exception)          # a generic `except Exception` retry never swallows it
cancel.bind(None)

# 2. worker threads of a job (ThreadPoolExecutor) see the job's cancel too
ev = threading.Event(); ev.set(); cancel.bind(ev)
with ThreadPoolExecutor(2) as ex:
    f = ex.submit(cancel.wrap(lambda: cancel.sleep(30)))
    try:
        f.result(timeout=5)
        raise SystemExit("worker not cancelled")
    except cancel.Cancelled:
        pass
cancel.bind(None)


# 3. the job: POST /api/jobs/{id}/cancel stops a running pipeline at its next wait; state 'cancelled'
def slow_pipeline(topo, cfg, fmg, emit, boot):
    emit("preflight", "running", "waiting")
    for _ in range(100):
        cancel.sleep(0.5)
    raise SystemExit("pipeline ran to the end")


main.pipeline = slow_pipeline
main.Fmg = lambda *a, **k: type("F", (), {"logout": lambda self: None})()
job = {"id": "cafe0001", "state": "running", "dry_run": False, "events": [], "created": "2026-11-26 12:00:00",
       "title": "Deployment of X", "steps": {"preflight": "pending", "adom": "pending"}}
main.JOBS[job["id"]] = job
fmgcfg = type("C", (), {"host": "10.0.0.1", "username": "admin", "password": "x", "api_token": None})()
th = threading.Thread(target=main._run, args=(job, {"fmgr": {}}, {}, fmgcfg, False, {}, lambda m: m), daemon=True)
main._start_job(job, th)
time.sleep(0.3)
assert job["state"] == "running"
r = main.job_cancel("cafe0001")
assert r["ok"] is True
th.join(5)
assert not th.is_alive(), "pipeline did not stop"
assert job["state"] == "cancelled", job["state"]
assert job["steps"]["preflight"] == "cancelled" and job["steps"]["adom"] == "pending", job["steps"]
assert any("Cancel requested" in e["msg"] for e in job["events"])
assert "Cleanup" in job["events"][-1]["msg"] and job["events"][-1]["status"] == "cancelled", job["events"][-1]
assert main._get_job("cafe0001")["state"] == "cancelled"

# cancelling a finished or unknown job: a clear error
for jid, code in (("cafe0001", 409), ("nope", 404)):
    try:
        main.job_cancel(jid)
        raise SystemExit(f"no {code}")
    except main.HTTPException as e:
        assert e.status_code == code, (jid, e.status_code)

# 4. the old job banner and the restart marker know the state
print("OK  cancel: interruptible waits (also in worker threads), POST cancel stops the job as 'cancelled', 409/404 otherwise")
