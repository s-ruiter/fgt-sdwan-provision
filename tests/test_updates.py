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
    def __init__(self, code, body, text="", headers=None):
        self.status_code, self._b, self.text, self.headers = code, body, text, headers or {}

    def json(self):
        return self._b


HEAD_URL = "https://api.github.com/repos/s-ruiter/fgt-sdwan-provision/commits/main"
CMP_URL = f"https://api.github.com/repos/s-ruiter/fgt-sdwan-provision/compare/{own}...main"
calls, GH = [], {"head": "b" * 40, "etag": 'W/"e1"'}


def gh(url, timeout=None, headers=None):
    """Fake GitHub: main's head (sha media type, ETag -> 304 when unchanged) and the compare."""
    calls.append((url, dict(headers or {})))
    if url == HEAD_URL:
        if (headers or {}).get("If-None-Match") == GH["etag"]:
            return R(304, None)
        return R(200, None, text=GH["head"], headers={"ETag": GH["etag"]})
    assert url == CMP_URL, url
    cs = [{"sha": "a" * 40, "commit": {"message": "Feature one\n\nbody"}}, {"sha": "b" * 40, "commit": {"message": "Fix two"}}]
    if GH["head"] == "c" * 40:
        cs.append({"sha": "c" * 40, "commit": {"message": "Three"}})
    return R(200, {"ahead_by": len(cs), "status": "ahead", "commits": cs})


updates.CACHE.clear()
c = updates.check(repo, fetch=gh)
assert [u for u, _ in calls] == [HEAD_URL, CMP_URL], calls
assert calls[0][1].get("Accept") == "application/vnd.github.sha"
assert c["current"] == own[:7] and c["behind"] == 2 and c["latest"] == "bbbbbbb" and c["error"] is None
assert c["commits"] == [{"sha": "aaaaaaa", "title": "Feature one"}, {"sha": "bbbbbbb", "title": "Fix two"}]
updates.check(repo, fetch=gh)
assert len(calls) == 2                                                       # cached (short)
assert 60 <= updates.CACHE_S <= 120, "a new commit on main shows within two minutes; at most 60 GitHub requests/hour"
# cache expired, main unchanged: only the small conditional request (304), no compare
updates.CACHE["t"] -= updates.CACHE_S + 1
c2 = updates.check(repo, fetch=gh)
assert [u for u, _ in calls[2:]] == [HEAD_URL] and calls[2][1].get("If-None-Match") == 'W/"e1"' and c2 == c, calls[2:]
# a new commit on main: the next check (after the short cache) sees it
GH.update(head="c" * 40, etag='W/"e2"')
updates.CACHE["t"] -= updates.CACHE_S + 1
c3 = updates.check(repo, fetch=gh)
assert [u for u, _ in calls[3:]] == [HEAD_URL, CMP_URL] and c3["behind"] == 3 and c3["latest"] == "ccccccc", c3
print("OK  versie: korte cache; main ongewijzigd = alleen de kleine check, nieuwe commit op main direct zichtbaar")

updates.CACHE.clear()
same = updates.check(repo, fetch=lambda u, **k: R(200, {"ahead_by": 0, "commits": [], "status": "identical"}, text=own, headers={}))
assert same["behind"] == 0 and same["error"] is None


def down(url, **k):
    raise OSError("no route")


updates.CACHE.clear()
d = updates.check(repo, fetch=down)
assert d["behind"] == 0 and "could not check" in d["error"]
updates.CACHE.clear()
n = updates.check(tempfile.mkdtemp(), fetch=gh)
assert n["current"] is None and n["behind"] == 0 and n["error"]
print("OK  versie: eigen commit vs GitHub main (gecachet), achterstand + titels, GitHub onbereikbaar / geen git = geen update")

ran = []
updates.start("/opt/fgt-sdwan-provision", run=lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
assert ran == [["systemd-run", "--unit", "fgt-sdwan-update", "--collect", "bash", "/opt/fgt-sdwan-provision/ct/install.sh"]], ran
ran2 = []
updates.start("/opt/x", run=lambda cmd, **k: ran2.append(cmd) or subprocess.CompletedProcess(cmd, 0), status_file="/var/lib/s")
assert ran2[0][-2:] == ["-c", 'bash "/opt/x/ct/install.sh"; echo $? > "/var/lib/s"'], ran2

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

c = TestClient(main.app)
main.UPDATE_ACTIVE = lambda: False
main.APP_DIR = repo
GH.update(head="b" * 40, etag='W/"e1"')
updates.CACHE.clear()
main.UPDATE_FETCH = gh
v = c.get("/api/version").json()
assert v["behind"] == 2 and v["commits"][0]["title"] == "Feature one"
main.JOBS["x"] = {"state": "running", "dry_run": False}
assert c.post("/api/update").status_code == 409
main.JOBS.clear()
started = []
main.UPDATE_RUN = lambda cmd, **k: started.append(cmd) or subprocess.CompletedProcess(cmd, 0)
r = c.post("/api/update")
assert r.status_code == 200 and started and f"{repo}/ct/install.sh" in started[0][-1], (r.text, started)
updates.CACHE.clear()
main.UPDATE_FETCH = lambda u, **k: R(200, {"ahead_by": 0, "commits": []}, text=own)
assert c.post("/api/update").status_code == 409                               # nothing to update
assert c.get("/api/about").status_code == 200                                  # the Progress page still uses the steps
print("OK  API: /api/version, /api/update via systemd-run; geweigerd tijdens een uitrol of als er niets nieuws is")

# while the update unit runs: no deployment or cleanup (the restart would kill it)
main.UPDATE_ACTIVE = lambda: True
r = c.post("/api/deploy", json={"topology": {}, "fmg": {"host": "x"}, "scope": "tpl", "secrets": {}})
assert r.status_code == 409 and "updating" in r.text.lower(), r.text
r = c.post("/api/cleanup", json={"server_id": 1, "node": "n", "confirm": "DELETE"})
assert r.status_code == 409 and "updating" in r.text.lower(), r.text
main.UPDATE_ACTIVE = lambda: False
# the result of the last update is reported (a failed install.sh does not restart the service)
assert ran[0][:5] == ["systemd-run", "--unit", "fgt-sdwan-update", "--collect", "bash"]
st = os.path.join(os.environ["DATA_DIR"], "update.status")
with open(st, "w") as f:
    f.write("1\n")
updates.CACHE.clear()
main.UPDATE_FETCH = gh
v = c.get("/api/version").json()
assert v["last_update"]["rc"] == 1 and v["last_update"]["at"] > 0, v
print("OK  update: geen uitrol/cleanup tijdens een update; resultaat van de laatste update gemeld")
