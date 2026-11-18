"""Tiny fake FortiManager JSON-RPC endpoint for exercising the pipeline locally.
NOT a FortiManager: it only answers the calls the pipeline makes."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"adoms": set(), "devices": set(), "scripts": [], "calls": [], "bodies": [],
         # FortiAnalyzer features on by default: enabling them reboots a real FMG (tests/test_fmg_settings.py covers that)
         "cli": {"cli/global/system/global": {"faz-status": "enable"}}}
MISSING = lambda u: {"status": {"code": -3, "message": "Object does not exist"}, "url": u}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        p = body["params"][0]
        if not p["url"].startswith("/"):                  # like a real FortiManager
            out = json.dumps({"id": body["id"], "result": [{"status": {"code": -11, "message": "No permission for the resource"}, "url": p["url"]}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            return
        url = p["url"].lstrip("/")
        m = body["method"]
        STATE["calls"].append((m, url))
        STATE["bodies"].append((m, url, p.get("data")))

        def ok(data=None):
            r = {"status": {"code": 0, "message": "OK"}, "url": p["url"]}
            if data is not None:
                r["data"] = data
            return r
        res = ok()
        parts = url.split("/")
        if url.startswith("pm/config/device/") and url.endswith("/global/system/interface"):
            ifs = STATE.setdefault("ifaces", {}).setdefault(parts[3], [{"name": "port1", "type": "physical"}])
            if m == "get":
                res = ok(list(ifs))
            elif m == "add":
                for i in p.get("data") or []:
                    if any(x["name"] == i["name"] for x in ifs):
                        res = {"status": {"code": -2, "message": "Object already exists"}, "url": p["url"]}
                        break
                    ifs.append(dict(i))
        elif url.startswith("cli/global/"):
            cli = STATE.setdefault("cli", {}).setdefault(url, {})
            if m == "get":
                res = ok(dict(cli))
            elif m in ("set", "update"):
                cli.update(p.get("data") or {})
                STATE.setdefault("cli_sets", []).append((url, dict(p.get("data") or {})))
        elif url == "sys/status":
            res = ok({"Version": "v8.0.0-build0001 260101 (GA)", "Hostname": "mock-fmg", "Serial Number": "FMVMELTM26000001"})
        elif m == "get" and url.startswith("dvmdb/adom/") and len(parts) == 3:
            res = ok({"name": parts[2]}) if parts[2] in STATE["adoms"] else MISSING(p["url"])
        elif url == "dvmdb/adom" and m == "add":
            STATE["adoms"].add(p["data"][0]["name"])
        elif m == "get" and len(parts) == 5 and parts[3] == "device":
            res = ok({"name": parts[4], "conn_status": 1}) if parts[4] in STATE["devices"] else MISSING(p["url"])
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
                          "devices": sorted(STATE["devices"]), "adoms": sorted(STATE["adoms"]), "sets": STATE.get("sets", 0), "bodies": STATE["bodies"]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8099), H).serve_forever()
