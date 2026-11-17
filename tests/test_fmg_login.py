"""FortiManager login: URL form (a real FMG answers -11 without the leading slash), retry behaviour.  python -m tests.test_fmg_login"""
import os, sys, threading
from http.server import HTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from app import fmg as fmgmod
from app.fmg import Fmg, FmgError, _login_retry
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
# -11 (no permission) is treated the same way
s = Stub(["exec /sys/login/user: (-11) No permission for the resource"] * 20)
try:
    _login_retry(s, 20, 15)
    raise SystemExit("had moeten falen")
except FmgError:
    assert s.n == 2
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
print("OK  FMG-login: pad met /, geweigerd wachtwoord stopt na 2 pogingen, lockout (-23) wordt uitgewacht, niet-gereed wordt herhaald")
