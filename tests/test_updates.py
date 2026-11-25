"""Self-update: the tool compares its own commit with GitHub main and can run ct/install.sh (via systemd-run) to update."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import updates  # noqa: E402

repo = tempfile.mkdtemp()
subprocess.run(["git", "init", "-q", repo], check=True)
subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
own = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
assert updates.current(repo) == own and updates.current(tempfile.mkdtemp()) is None


class R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


calls = []


def fetch_behind(url, timeout=None, headers=None):
    calls.append(url)
    return R(200, {"behind_by": 0, "ahead_by": 2, "status": "ahead",
                   "commits": [{"sha": "a" * 40, "commit": {"message": "Feature one\n\nbody"}},
                               {"sha": "b" * 40, "commit": {"message": "Fix two"}}]})


updates.CACHE.clear()
c = updates.check(repo, fetch=fetch_behind)
assert calls[0] == f"https://api.github.com/repos/s-ruiter/fgt-sdwan-provision/compare/{own}...main", calls
assert c["current"] == own[:7] and c["behind"] == 2 and c["latest"] == "bbbbbbb" and c["error"] is None
assert c["commits"] == [{"sha": "aaaaaaa", "title": "Feature one"}, {"sha": "bbbbbbb", "title": "Fix two"}]
updates.check(repo, fetch=fetch_behind)
assert len(calls) == 1                                                       # cached

updates.CACHE.clear()
same = updates.check(repo, fetch=lambda u, **k: R(200, {"ahead_by": 0, "commits": [], "status": "identical"}))
assert same["behind"] == 0 and same["error"] is None


def down(url, **k):
    raise OSError("no route")


updates.CACHE.clear()
d = updates.check(repo, fetch=down)
assert d["behind"] == 0 and "could not check" in d["error"]
updates.CACHE.clear()
n = updates.check(tempfile.mkdtemp(), fetch=fetch_behind)
assert n["current"] is None and n["behind"] == 0 and n["error"]
print("OK  versie: eigen commit vs GitHub main (gecachet), achterstand + titels, GitHub onbereikbaar / geen git = geen update")

ran = []
updates.start("/opt/fgt-sdwan-provision", run=lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
assert ran == [["systemd-run", "--unit", "fgt-sdwan-update", "--collect", "bash", "/opt/fgt-sdwan-provision/ct/install.sh"]], ran

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

c = TestClient(main.app)
main.APP_DIR = repo
updates.CACHE.clear()
main.UPDATE_FETCH = fetch_behind
v = c.get("/api/version").json()
assert v["behind"] == 2 and v["commits"][0]["title"] == "Feature one"
main.JOBS["x"] = {"state": "running", "dry_run": False}
assert c.post("/api/update").status_code == 409
main.JOBS.clear()
started = []
main.UPDATE_RUN = lambda cmd, **k: started.append(cmd) or subprocess.CompletedProcess(cmd, 0)
r = c.post("/api/update")
assert r.status_code == 200 and started and started[0][-1] == f"{repo}/ct/install.sh", (r.text, started)
updates.CACHE.clear()
main.UPDATE_FETCH = lambda u, **k: R(200, {"ahead_by": 0, "commits": []})
assert c.post("/api/update").status_code == 409                               # nothing to update
assert c.get("/api/about").status_code == 200                                  # the Progress page still uses the steps
print("OK  API: /api/version, /api/update via systemd-run; geweigerd tijdens een uitrol of als er niets nieuws is")
