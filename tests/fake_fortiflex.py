"""Fake FortiFlex API v2 + OAuth endpoint for tests. Written from the API documentation; NOT Fortinet."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {}
USER, PASSWORD = "apiuser", "apipass"


def reset():
    STATE.clear()
    STATE.update(calls=[], seq=0, fail=None, ents={
        "FGVMELTM26000001": {"configId": 10, "description": "fgt-sdwan SDWAN site1-H1", "status": "ACTIVE", "tokenStatus": "USED"},
        "FGVMELTM26000002": {"configId": 10, "description": "oud-lab", "status": "ACTIVE", "tokenStatus": "NOTUSED"},
        "FGVMELTM26000003": {"configId": 10, "description": "fgt-sdwan SDWAN site1-1", "status": "STOPPED", "tokenStatus": "USED"},
        "FGVMELTM26000004": {"configId": 10, "description": "fgt-sdwan SDWAN site1-2", "status": "EXPIRED", "tokenStatus": "USED"},
        "FGVMELTM26000009": {"configId": 11, "description": "andere config", "status": "ACTIVE", "tokenStatus": "NOTUSED"},
        "FMGVMSTM26000001": {"configId": 20, "description": "fgt-sdwan SDWAN fmg", "status": "PENDING", "tokenStatus": "NOTUSED"},
    })
    for sn, e in STATE["ents"].items():
        e.update(serialNumber=sn, token=f"TOK{sn[-6:]}AAAAAAAAAAA", accountId=1, endDate="2027-12-31 00:00:00")


reset()


def _ent(sn):
    return dict(STATE["ents"][sn])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data):
        b = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        path = self.path
        STATE["calls"].append((path, body))
        if path == "/api/v1/oauth/token/":
            if body.get("username") == USER and body.get("password") == PASSWORD and body.get("client_id") == "flexvm":
                return self._send(200, {"access_token": "AT123", "expires_in": 3600, "token_type": "Bearer"})
            return self._send(401, {"error": "invalid_grant", "error_description": "Invalid credentials given."})
        if self.headers.get("Authorization") != "Bearer AT123":
            return self._send(401, {"status": -1, "message": "Unauthorized", "error": "Unauthorized"})
        p = path.replace("/ES/api/fortiflex/v2", "")
        if STATE["fail"] and STATE["fail"] in p:
            return self._send(200, {"status": -1, "message": "Request failed", "error": "simulated: not enough points"})
        ok = {"status": 0, "message": "Request processed successfully", "error": None}
        e = STATE["ents"]
        if p == "/programs/list":
            return self._send(200, dict(ok, programs=[{"serialNumber": "ELAVMS0000001234", "accountId": 1,
                                                       "startDate": "2026-01-01", "endDate": "2027-12-31"}]))
        if p == "/configs/list":
            return self._send(200, dict(ok, configs=[
                {"id": 10, "name": "FGT 2CPU UTP", "status": "ACTIVE", "productType": {"id": 1, "name": "FortiGate VM"},
                 "parameters": [{"id": 1, "name": "CPU", "value": "2"}, {"id": 2, "name": "SERVICEPACK", "value": "UTP"}]},
                {"id": 11, "name": "FGT 1CPU", "status": "ACTIVE", "productType": {"id": 1, "name": "FortiGate VM"}, "parameters": []},
                {"id": 20, "name": "FMG 10 dev", "status": "ACTIVE", "productType": {"id": 3, "name": "FortiManager VM"}, "parameters": []}]))
        if p == "/entitlements/list":
            return self._send(200, dict(ok, entitlements=[_ent(s) for s, v in e.items() if v["configId"] == body["configId"]]))
        if p == "/entitlements/vm/create":
            STATE["seq"] += 1
            sn = f"FGVMELTM2699{STATE['seq']:04d}" if body["configId"] != 20 else f"FMGVMSTM2699{STATE['seq']:04d}"
            e[sn] = {"serialNumber": sn, "configId": body["configId"], "description": body.get("description", ""),
                     "status": "PENDING", "tokenStatus": "NOTUSED", "token": f"NEW{STATE['seq']:04d}BBBBBBBBBBBBB",
                     "accountId": 1, "endDate": "2027-12-31 00:00:00"}
            return self._send(200, dict(ok, entitlements=[_ent(sn)]))
        sn = body.get("serialNumber")
        if sn not in e:
            return self._send(200, {"status": -1, "message": "Not found", "error": f"serial {sn} not found"})
        if p == "/entitlements/vm/token":
            STATE["seq"] += 1
            e[sn].update(token=f"RGN{STATE['seq']:04d}CCCCCCCCCCCCC", tokenStatus="NOTUSED")
        elif p == "/entitlements/reactivate":
            e[sn]["status"] = "ACTIVE"
        elif p == "/entitlements/update":
            e[sn].update(description=body["description"], configId=body["configId"])
        elif p == "/entitlements/stop":
            e[sn]["status"] = "STOPPED"
        else:
            return self._send(404, {"status": -1, "error": "unknown"})
        return self._send(200, dict(ok, entitlements=[_ent(sn)]))


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
