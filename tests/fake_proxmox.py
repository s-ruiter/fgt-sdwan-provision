"""Fake Proxmox VE API for tests. Implements only what app/pve.py + provision_vms.py call.
NOT Proxmox: behaviour is written from the public API docs. Run standalone: python tests/fake_proxmox.py 8097"""
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {}


def reset():
    STATE.clear()
    STATE.update(vms={}, cts={}, ct_delete_params=None, uploads={}, calls=[], fail_create_at=None, creates=0, nextid=100, upid=0)


reset()
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, data=None, code=200, reason=None):
        out = json.dumps({"data": data}).encode()
        self.send_response(code, reason)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _upid(self):
        STATE["upid"] += 1
        return f"UPID:pve1:0000{STATE['upid']:04d}:00000000:00000000:task:100:root@pam!tok:"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _route(self, method):
        u = urlparse(self.path)
        path = re.sub(r"^/api2/json", "", u.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        STATE["calls"].append((method, path))
        if path == "/__auth":
            out = json.dumps({"auth": STATE.get("auth")}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        STATE["auth"] = self.headers.get("Authorization", "")
        if not self.headers.get("Authorization", "").startswith("PVEAPIToken="):
            return self._send(None, 401, "No ticket")
        body = self._body()
        with LOCK:
            return self._handle(method, path, q, body)

    def _handle(self, method, path, q, body):
        vms = STATE["vms"]
        if path == "/version":
            return self._send({"version": "9.0.3"})
        if path == "/nodes":
            return self._send([{"node": "pve1"}])
        if path == "/storage":
            if STATE.get("storage_hidden"):                     # API token without Datastore.Audit
                return self._send([])
            imp = "" if STATE.get("no_import") else ",import"
            return self._send([{"storage": "local", "content": "iso,vztmpl,backup" + imp},
                               {"storage": "local-lvm", "content": "images,rootdir"}])
        if path == "/nodes/pve1/storage" and method == "GET":
            allst = [("local", "iso,vztmpl,import,backup", 1, 90e9), ("local-lvm", "images,rootdir", 1, 400e9),
                     ("old", "images", 0, 0)]
            if STATE.get("shared_iso"):                       # optional: a shared (NFS) ISO storage
                allst.append(("nfs", "iso", 1, 1e9))
            want = q.get("content")
            return self._send([{"storage": n, "content": c, "active": a, "avail": av, "shared": int(n == "nfs")}
                               for n, c, a, av in allst if want in c.split(",")])
        if path == "/nodes/pve1/network":
            return self._send([{"iface": "vmbr0"}, {"iface": "vmbr1"}])
        if path == "/cluster/nextid":
            used = {int(v) for v in vms} | {int(c) for c in STATE["cts"]}     # PVE: VMs and CTs share the id space
            n = STATE["nextid"]                                               # PVE: the LOWEST free id from 100
            while n in used:
                n += 1
            return self._send(str(n))
        if path == "/cluster/resources":
            return self._send([{"vmid": v["vmid"], "name": v["name"], "tags": v["params"].get("tags", ""),
                                "status": v["status"], "type": "qemu"} for v in vms.values()] +
                              [{"vmid": c["vmid"], "name": c["params"].get("hostname"), "tags": c["params"].get("tags", ""),
                                "status": c["status"], "type": "lxc"} for c in STATE["cts"].values()])
        cts = STATE["cts"]
        if path == "/nodes/pve1/lxc" and method == "POST":
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if not f.get("ostemplate", "").startswith(("local:vztmpl/",)) or f.get("vmid") in cts or f.get("vmid") in vms:
                return self._send(None, 500, "bad ostemplate or vmid in use")
            if STATE.get("fail_ct_create"):
                return self._send(None, 500, "ct create failed: simulated")
            cts[f["vmid"]] = {"vmid": int(f["vmid"]), "params": f, "status": "stopped"}
            return self._send(self._upid())
        m = re.fullmatch(r"/nodes/pve1/lxc/(\d+)(/.*)?", path)
        if m:
            ct, sub = cts.get(m.group(1)), m.group(2) or ""
            if ct is None:
                return self._send(None, 500, "no such CT")
            if method == "DELETE" and not sub:
                if STATE.get("fail_delete_ct") == m.group(1):
                    return self._send(None, 500, "CT is locked (simulated)")
                STATE["ct_delete_params"] = q
                cts.pop(m.group(1))
                return self._send(self._upid())
            if method == "POST" and sub in ("/status/start", "/status/stop"):
                ct["status"] = "running" if sub.endswith("start") else "stopped"
                return self._send(self._upid())
            if method == "GET" and sub == "/status/current":
                return self._send({"status": ct["status"]})
            if method == "PUT" and sub == "/config":
                ct["params"].update({k: v[0] for k, v in parse_qs(body.decode()).items()})
                return self._send(None)
        m = re.fullmatch(r"/nodes/pve1/storage/([^/]+)/upload", path)
        if m and method == "POST":
            ctype = self.headers["Content-Type"]
            boundary = ctype.split("boundary=")[1].encode()
            fields, fname, payload = {}, None, b""
            for part in body.split(b"--" + boundary):
                if b"Content-Disposition" not in part:
                    continue
                head, _, data = part.partition(b"\r\n\r\n")
                data = data[:-2] if data.endswith(b"\r\n") else data
                nm = re.search(rb'name="([^"]+)"', head).group(1).decode()
                if nm == "filename":
                    fname, payload = re.search(rb'filename="([^"]+)"', head).group(1).decode(), data
                else:
                    fields[nm] = data.decode()
            content = fields.get("content")
            if content not in ("iso", "import"):
                return self._send(None, 400, "bad content")
            STATE["uploads"][f"{m.group(1)}:{content}/{fname}"] = payload
            return self._send(self._upid())
        m = re.fullmatch(r"/nodes/pve1/storage/([^/]+)/content", path)
        if m and method == "GET":
            pre = f"{m.group(1)}:{q.get('content')}/"
            return self._send([{"volid": k, "size": len(v)} for k, v in STATE["uploads"].items() if k.startswith(pre)])
        m = re.fullmatch(r"/nodes/pve1/storage/([^/]+)/content/(.+)", path)
        if m and method == "DELETE":
            STATE["uploads"].pop(m.group(2), None)
            return self._send(self._upid())
        if path == "/nodes/pve1/qemu" and method == "POST":
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            STATE["creates"] += 1
            if STATE["fail_create_at"] and STATE["creates"] == STATE["fail_create_at"]:
                return self._send(None, 500, "create failed: simulated")
            src = re.search(r"import-from=(\S+)", f.get("scsi0", ""))
            if not src or src.group(1) not in STATE["uploads"]:
                return self._send(None, 500, "import source not found")
            vmid = int(f["vmid"])
            if str(vmid) in vms:
                return self._send(None, 500, "VM already exists")
            vms[str(vmid)] = {"vmid": vmid, "name": f["name"], "params": f, "status": "stopped"}
            return self._send(self._upid())
        m = re.fullmatch(r"/nodes/pve1/qemu/(\d+)(/.*)?", path)
        if m:
            vmid, sub = m.group(1), m.group(2) or ""
            vm = vms.get(vmid)
            if vm is None:
                return self._send(None, 500, "no such VM")
            if method == "DELETE" and not sub:
                if STATE.get("fail_delete") == vmid:
                    return self._send(None, 500, "delete failed: simulated")
                vms.pop(vmid)
                return self._send(self._upid())
            if method == "POST" and sub in ("/status/start", "/status/stop"):
                vm["status"] = "running" if sub.endswith("start") else "stopped"
                return self._send(self._upid())
            if method == "GET" and sub == "/status/current":
                return self._send({"status": vm["status"]})
            if method == "PUT" and sub == "/config":
                vm["params"].update({k: v[0] for k, v in parse_qs(body.decode()).items()})
                return self._send(None)
        m = re.fullmatch(r"/nodes/pve1/tasks/([^/]+)/status", path)
        if m:
            return self._send({"status": "stopped", "exitstatus": "OK"})
        return self._send(None, 501, f"not implemented: {method} {path}")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


def start(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    start(int(sys.argv[1]) if len(sys.argv) > 1 else 8097)
    threading.Event().wait()
