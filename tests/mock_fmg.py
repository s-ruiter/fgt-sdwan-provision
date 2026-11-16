"""Tiny fake FortiManager JSON-RPC endpoint for exercising the pipeline locally.
NOT a FortiManager: it only answers the calls the pipeline makes."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"adoms": set(), "devices": set(), "scripts": [], "calls": []}
MISSING = lambda u: {"status": {"code": -3, "message": "Object does not exist"}, "url": u}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        p = body["params"][0]
        url = p["url"].lstrip("/")
        m = body["method"]
        STATE["calls"].append((m, url))

        def ok(data=None):
            r = {"status": {"code": 0, "message": "OK"}, "url": p["url"]}
            if data is not None:
                r["data"] = data
            return r
        res = ok()
        parts = url.split("/")
        if url == "sys/status":
            res = ok({"Version": "v8.0.0-build0001 260101 (GA)", "Hostname": "mock-fmg"})
        elif m == "get" and url.startswith("dvmdb/adom/") and len(parts) == 3:
            res = ok({"name": parts[2]}) if parts[2] in STATE["adoms"] else MISSING(p["url"])
        elif url == "dvmdb/adom" and m == "add":
            STATE["adoms"].add(p["data"][0]["name"])
        elif m == "get" and len(parts) == 5 and parts[3] == "device":
            res = ok({"name": parts[4]}) if parts[4] in STATE["devices"] else MISSING(p["url"])
        elif url == "dvm/cmd/add/device":
            STATE["devices"].add(p["data"]["device"]["name"])
            res = ok({"taskid": 101})
        elif m == "get" and len(parts) == 5 and parts[3] == "script":
            hit = [s for s in STATE["scripts"] if s["name"] == parts[4]]
            res = ok(hit[0]) if hit else MISSING(p["url"])
        elif m == "set" and len(parts) == 5 and parts[3] == "script":
            for s in STATE["scripts"]:
                if s["name"] == parts[4]:
                    s.update(p["data"])
            STATE["sets"] = STATE.get("sets", 0) + 1
        elif url.endswith("/script") and m == "add":
            STATE["scripts"].append(p["data"][0])
        elif url.endswith("/script/execute") or url.startswith("securityconsole/"):
            res = ok({"task": 102})
        elif url.startswith("task/task/"):
            res = ok({"state": 4, "percent": 100, "num_err": 0})
        out = json.dumps({"id": body["id"], "result": [res], "session": "mocksession"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        out = json.dumps({"calls": STATE["calls"], "scripts": [s["name"] for s in STATE["scripts"]],
                          "script_sample": STATE["scripts"][0]["content"][:200] if STATE["scripts"] else "",
                          "devices": sorted(STATE["devices"]), "adoms": sorted(STATE["adoms"]), "sets": STATE.get("sets", 0)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8099), H).serve_forever()
