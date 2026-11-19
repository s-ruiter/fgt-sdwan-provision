"""FortiManager login: URL form (a real FMG answers -11 without the leading slash), retry behaviour.  python -m tests.test_fmg_login"""
import os, sys, threading
from http.server import HTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from app import fmg as fmgmod
from app.fmg import Fmg, FmgError, _login_retry, wait_fmg_ready, _license_state
from tests import mock_fmg

srv = HTTPServer(("127.0.0.1", 8097 + 3), mock_fmg.H)                 # 8100
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:8100"

# 1. the strict mock behaves like a real FMG: no leading slash -> -11
r = requests.post(URL + "/jsonrpc", json={"id": 1, "method": "exec", "params": [{"url": "sys/login/user", "data": {}}]}).json()
assert r["result"][0]["status"]["code"] == -11, r
# 2. our client normalises the path, so login/logout work
f = Fmg(URL, "admin", "pw")
f.login()
assert f.session == "mocksession"
f.logout()

# 3. retry policy (sleep patched out)
slept = []
fmgmod.time.sleep = lambda s: slept.append(s)


class Stub:
    def __init__(self, errors):
        self.errors, self.n = list(errors), 0

    def login(self):
        self.n += 1
        if self.errors:
            raise FmgError(self.errors.pop(0))


# wrong password: stops after two refusals, with a clear message (no hammering -> no account lockout)
s = Stub(["exec /sys/login/user: (-22) Login fail"] * 20)
try:
    _login_retry(s, 20, 15)
    raise SystemExit("had moeten falen")
except FmgError as e:
    assert s.n == 2 and "geweigerd" in str(e) and "blokkeert" in str(e), (s.n, str(e))
# -11 (no permission): the factory admin has rpc-permit none -> the message says how to fix it
s = Stub(["exec /sys/login/user: (-11) No permission for the resource"] * 20)
try:
    _login_retry(s, 20, 15)
    raise SystemExit("had moeten falen")
except FmgError as e:
    assert s.n == 2 and "rpc-permit read-write" in str(e), str(e)
# not ready yet (connection errors): keeps trying, then succeeds
s = Stub(["exec /sys/login/user: HTTPSConnectionPool refused"] * 3)
slept.clear(); _login_retry(s, 20, 15); assert s.n == 4 and slept == [15, 15, 15], (s.n, slept)
# lockout (-23): waits at least 70 s, then succeeds
s = Stub(["exec /sys/login/user: (-23) Reach login limit"])
slept.clear(); _login_retry(s, 20, 15); assert s.n == 2 and slept == [70], slept
# connection problems for the whole time: the last error is raised
s = Stub(["x: refused"] * 5)
try:
    _login_retry(s, 5, 1)
    raise SystemExit("had moeten falen")
except FmgError as e:
    assert "refused" in str(e) and s.n == 5

# progress: every failed attempt is reported (a long wait must be visible in the job log)
msgs = []
s = Stub(["x: refused"] * 2)
_login_retry(s, 20, 15, msgs.append); assert len(msgs) == 2 and "inlogpoging 1/20" in msgs[0], msgs

# Day-0 readiness: login works, but the licence is not active yet (FMG still has to reboot) -> wait, then continue
assert _license_state({"License Status": "Valid"})[0]
assert not _license_state({"License Status": "Invalid"})[0]
assert not _license_state({"Serial Number": "FMG-VM0000000000"})[0]
assert _license_state({"Version": "v8.0.0"})[0]                     # unknown fields: do not block


class StatusStub(Stub):
    def __init__(self, statuses):
        super().__init__([])
        self.statuses, self.timeout, self.session, self.logouts = list(statuses), 60, None, 0

    def call(self, method, url, data=None):
        return {"data": self.statuses.pop(0)}

    def logout(self):
        self.logouts += 1


f = StatusStub([{"License Status": "Invalid"}, {"License Status": "Valid", "Version": "v8.0.0"}])
msgs = []
st = wait_fmg_ready(f, {"login_delay": 0}, msgs.append)
assert st["License Status"] == "Valid" and f.n == 2 and f.logouts == 1 and f.timeout == 60, (st, f.n)
assert any("licentie" in m for m in msgs), msgs
# never licensed: stops with a clear message after the timeout
f = StatusStub([{"License Status": "Invalid"}] * 5)
try:
    wait_fmg_ready(f, {"login_delay": 0, "fmg_ready_timeout": -1}, msgs.append)
    raise SystemExit("had moeten falen")
except FmgError as e:
    assert "nog niet gelicenseerd" in str(e), str(e)
print("OK  FMG-login: pad met /, geweigerd wachtwoord stopt na 2 pogingen, lockout (-23) wordt uitgewacht, niet-gereed wordt herhaald")

# wait_online: FortiManager answers enums as text with verbose=1 ("up" / "down" / "UNKNOWN"), older code used 1
from app.fmg import wait_online  # noqa: E402


class _F:
    dry = False

    def __init__(self, st):
        self.st = st

    def call(self, m, url):
        return {"data": {"conn_status": self.st[url.rsplit("/", 1)[1]]}}


left = wait_online(_F({"a": "up", "b": 1, "c": "UNKNOWN", "d": "down"}), "X", ["a", "b", "c", "d"], 0, lambda *x: None, poll=0)
assert left == ["c", "d"], left
print("OK  wait_online: 'up' (verbose) en 1 tellen als online; UNKNOWN/down niet")
