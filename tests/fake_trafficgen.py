"""Fake Traffic-Gen web API (plain HTTP) with the same shape as Traffic-Gen's webui.py: POST /api/login {password}
sets cookie tg, POSTs need header X-TG: 1, GET /api/state|config|apps|log, POST /api/start|stop|config. NOT Traffic-Gen."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {}


def reset(password="Tg-Admin-pw1!"):
    STATE.clear()
    STATE.update(password=password, running=False, logins=0, started_with=None,
                 config={"duration_minutes": 10, "threads_per_ip": 2, "intensity": "medium",
                         "groups": {"office365": 25, "collab": 20, "sdwan": 5, "malware": 0},
                         "disabled_apps": [], "networks": [{"name": "site1-1-lan1"}], "tls_verify": False})


reset()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, extra=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return "tg=good" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "login required"})
        if u.path == "/api/state":
            s = {"running": STATE["running"], "version": "fake", "has_run": STATE["started_with"] is not None}
            if s["has_run"]:
                s.update(totals={"requests": 12, "ok": 10, "blocked": 1, "error": 1, "bytes": 5000},
                         apps={"Teams": {"requests": 7, "ok": 7, "bytes": 3000}}, src={"10.0.1.20": {"requests": 12}},
                         series=[[1.0, 12, 5000, 1, 1]], intensity="medium", sources=10, deadline=None, started=1.0)
            return self._send(200, s)
        if u.path == "/api/config":
            return self._send(200, STATE["config"])
        if u.path == "/api/apps":
            return self._send(200, {"collab": [{"name": "Teams", "weight": 1, "targets": 3}]})
        if u.path == "/api/log":
            n = int(parse_qs(u.query).get("n", ["60"])[0])
            return self._send(200, [{"ts": "10:00:00", "src": "10.0.1.20", "app": "Teams", "url": "https://x", "result": "ok"}][:n])
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if self.headers.get("X-TG") != "1":
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/login":
            STATE["logins"] += 1
            if body.get("password") == STATE["password"]:
                return self._send(200, {"ok": True}, {"Set-Cookie": "tg=good; Path=/; HttpOnly"})
            return self._send(401, {"error": "Incorrect password."})
        if not self._authed():
            return self._send(401, {"error": "login required"})
        if u.path == "/api/start":
            if STATE["running"]:
                return self._send(400, {"error": "A test is already running."})
            STATE["running"], STATE["started_with"] = True, body
            return self._send(200, {"ok": True})
        if u.path == "/api/stop":
            STATE["running"] = False
            return self._send(200, {"ok": True})
        if u.path == "/api/config":
            STATE["config"].update({k: v for k, v in body.items() if k in STATE["config"]})
            return self._send(200, STATE["config"])
        return self._send(404, {"error": "not found"})


def start(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
