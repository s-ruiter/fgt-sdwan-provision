"""Client for the web API of a deployed Traffic-Gen (https://<mgmt ip>:8443, self-signed certificate).

Traffic-Gen: POST /api/login {password} sets a session cookie; every POST needs the header X-TG: 1; GET /api/state,
/api/config, /api/apps, /api/log?n=; POST /api/start, /api/stop, /api/config. The tool logs in with the GUI password
it kept at deployment, and again when the session has expired (401)."""
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)       # Traffic-Gen's own self-signed certificate


class TrafficError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


class TrafficClient:
    def __init__(self, addr, password, scheme="https", timeout=10):
        self.addr, self.password, self.timeout = addr, password, timeout
        self.base = f"{scheme}://{addr}"
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers["X-TG"] = "1"

    def _do(self, method, path, **kw):
        try:
            return self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise TrafficError(f"Traffic-Gen not reachable at {self.addr} ({type(e).__name__})") from None

    def login(self):
        r = self._do("POST", "/api/login", json={"password": self.password})
        if r.status_code != 200:
            raise TrafficError(f"Traffic-Gen refused the login ({self._err(r)}): the GUI password kept at deployment "
                               "does not work; deploy Traffic-Gen again or change the password in its GUI back")

    @staticmethod
    def _err(r):
        try:
            return r.json().get("error") or f"HTTP {r.status_code}"
        except ValueError:
            return f"HTTP {r.status_code}"

    def call(self, method, path, **kw):
        r = self._do(method, path, **kw)
        if r.status_code == 401:
            self.login()
            r = self._do(method, path, **kw)
        if r.status_code != 200:
            raise TrafficError(f"Traffic-Gen: {self._err(r)}", 400 if r.status_code == 400 else 502)
        return r.json()

    def state(self):
        return self.call("GET", "/api/state")

    def config(self):
        return self.call("GET", "/api/config")

    def set_config(self, body):
        return self.call("POST", "/api/config", json=body)

    def apps(self):
        return self.call("GET", "/api/apps")

    def log(self, n=60):
        return self.call("GET", f"/api/log?n={int(n)}")

    def start(self, body):
        return self.call("POST", "/api/start", json=body)

    def stop(self):
        return self.call("POST", "/api/stop", json={})
