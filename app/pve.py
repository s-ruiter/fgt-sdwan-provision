"""Proxmox VE API client (API token only, PVE 9+): upload to `import`/`iso` storage, create/start/delete VMs."""
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
CHUNK = 1024 * 1024


class PveError(Exception):
    def __init__(self, detail, status=None):
        super().__init__(detail)
        self.detail, self.status = detail, status


class _MultipartBody:
    """File-like multipart body with a known length (Proxmox needs Content-Length) that reports progress."""

    def __init__(self, fields, filename, path, on_progress=None):
        self.boundary = "----fgtsdwan" + str(time.time_ns())
        head = "".join(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                       for k, v in fields.items())
        head += (f'--{self.boundary}\r\nContent-Disposition: form-data; name="filename"; filename="{filename}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n")
        self.head = head.encode()
        self.tail = f"\r\n--{self.boundary}--\r\n".encode()
        self.path, self.size = Path(path), Path(path).stat().st_size
        self.total = len(self.head) + self.size + len(self.tail)
        self.on_progress = on_progress
        self._it = None
        self.sent = 0
        self.t0 = time.monotonic()

    def __len__(self):
        return self.total

    def _gen(self):
        yield self.head
        done = 0
        with open(self.path, "rb") as fh:
            while True:
                c = fh.read(CHUNK)
                if not c:
                    break
                done += len(c)
                if self.on_progress:
                    self.on_progress(done / self.size if self.size else 1.0,
                                     done / max(time.monotonic() - self.t0, 1e-6))
                yield c
        yield self.tail

    def __iter__(self):
        return self._gen()

    def read(self, n=-1):
        if self._it is None:
            self._it = self._gen()
            self._buf = b""
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


class Pve:
    def __init__(self, url, token_id, token_secret, verify_tls=True, poll=1.0, timeout=30):
        self.base = url.rstrip("/") + "/api2/json"
        self.headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        self.verify, self.poll, self.timeout = verify_tls, poll, timeout

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", self.timeout)
        headers = dict(self.headers, **kw.pop("headers", {}))
        try:
            r = requests.request(method, self.base + path, headers=headers, verify=self.verify, **kw)
        except requests.RequestException as e:
            raise PveError(f"verbinding met Proxmox mislukt: {e}") from e
        if r.status_code >= 400:
            # PVE puts the message in the reason phrase for 5xx and leaves the body empty
            parts = [r.reason]
            try:
                b = r.json()
                parts.append(str(b.get("errors") or b.get("message") or ""))
            except ValueError:
                parts.append(r.text[:200])
            raise PveError(f"Proxmox {r.status_code}: " + " - ".join(p for p in parts if p), status=r.status_code)
        return r.json().get("data")

    # ---- discovery
    def check(self):
        v = str(self._req("GET", "/version")["version"])
        if int(v.split(".")[0]) < 9:
            raise PveError(f"Proxmox VE {v} wordt niet ondersteund (minimaal 9)")
        res = {"version": v, "nodes": self.nodes()}
        cfg = self._req("GET", "/storage") or []
        res["import_storages"] = [s["storage"] for s in cfg if "import" in str(s.get("content", "")).split(",")]
        res["iso_storages"] = [s["storage"] for s in cfg if "iso" in str(s.get("content", "")).split(",")]
        res["image_storages"] = [s["storage"] for s in cfg if "images" in str(s.get("content", "")).split(",")]
        if not res["import_storages"]:
            res["warning"] = "geen storage met content-type 'import' (Datacenter > Storage > Content > Import)"
        return res

    def nodes(self):
        return sorted(n["node"] for n in self._req("GET", "/nodes"))

    def bridges(self, node):
        rows = self._req("GET", f"/nodes/{node}/network", params={"type": "any_bridge"}) or []
        return sorted(b["iface"] for b in rows)

    def next_vmid(self):
        return int(self._req("GET", "/cluster/nextid"))

    def find_vms(self, tag):
        rows = self._req("GET", "/cluster/resources", params={"type": "vm"}) or []
        return [r for r in rows if tag in str(r.get("tags", "")).split(";")]

    # ---- storage
    def upload(self, node, storage, path, filename, content, on_progress=None):
        body = _MultipartBody({"content": content}, filename, path, on_progress)
        return self._req("POST", f"/nodes/{node}/storage/{storage}/upload", data=body, timeout=None,
                         headers={"Content-Type": f"multipart/form-data; boundary={body.boundary}",
                                  "Content-Length": str(len(body))})

    def delete_volume(self, node, storage, volid):
        self._req("DELETE", f"/nodes/{node}/storage/{storage}/content/{volid}")

    # ---- VMs
    def create_vm(self, node, vmid, name, params):
        return self._req("POST", f"/nodes/{node}/qemu", data={"vmid": vmid, "name": name, **params})

    def set_config(self, node, vmid, params):
        return self._req("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=params)

    def start_vm(self, node, vmid):
        return self._req("POST", f"/nodes/{node}/qemu/{vmid}/status/start")

    def delete_vm(self, node, vmid):
        return self._req("DELETE", f"/nodes/{node}/qemu/{vmid}",
                         params={"purge": 1, "destroy-unreferenced-disks": 1})

    def wait_task(self, node, upid, timeout=3600):
        end = time.monotonic() + timeout
        errors = 0
        while True:
            try:
                st = self._req("GET", f"/nodes/{node}/tasks/{upid}/status")
                errors = 0
            except PveError as e:
                if (e.status is None or e.status >= 500) and errors < 5:
                    errors += 1
                    time.sleep(self.poll)
                    continue
                raise
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise PveError(f"taak mislukt: {st.get('exitstatus')}")
                return
            if time.monotonic() > end:
                raise PveError("timeout bij wachten op Proxmox-taak")
            time.sleep(self.poll)
