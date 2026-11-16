"""FortiManager JSON-RPC client and the deployment pipeline."""
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FmgError(Exception):
    pass


class Fmg:
    def __init__(self, host, username=None, password=None, api_token=None, dry_run=False,
                 log=lambda *_: None):
        self.base = (host if host.startswith("http") else f"https://{host}").rstrip("/") + "/jsonrpc"
        self.u, self.p, self.token, self.dry = username, password, api_token, dry_run
        self.session, self.n, self.log = None, 0, log

    def call(self, method, url, data=None, **extra):
        self.n += 1
        param = {"url": url}
        if data is not None:
            param["data"] = data
        param.update(extra)
        body = {"id": self.n, "method": method, "params": [param], "verbose": 1}
        if self.session:
            body["session"] = self.session
        if self.dry and method != "get":
            self.log("debug", f"[dry-run] {method} {url}")
            return {"status": {"code": 0, "message": "dry-run"}, "url": url}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = requests.post(self.base, json=body, headers=headers, verify=False, timeout=60)
            j = r.json()
        except Exception as e:  # network / non-JSON
            raise FmgError(f"{method} {url}: {e}")
        res = (j.get("result") or [{}])[0]
        st = res.get("status", {})
        if st.get("code", -1) != 0:
            raise FmgError(f"{method} {url}: ({st.get('code')}) {st.get('message')}")
        res["_session"] = j.get("session")
        return res

    def login(self):
        if self.token:
            return
        if self.dry:
            self.log("info", "dry-run: login overgeslagen")
            return
        res = self.call("exec", "sys/login/user", {"user": self.u, "passwd": self.p})
        self.session = res.get("_session")
        if not self.session:
            raise FmgError("login: geen session ontvangen")

    def logout(self):
        if self.session and not self.dry:
            try:
                self.call("exec", "sys/logout")
            except FmgError:
                pass

    def exists(self, url):
        try:
            return bool(self.call("get", url).get("data"))
        except FmgError as e:
            if "-3" in str(e) or "No such" in str(e) or "Object does not exist" in str(e):
                return False
            raise

    def wait_task(self, task_id, timeout=600):
        if self.dry or not task_id:
            return {"state": 4, "percent": 100}
        end = time.time() + timeout
        while time.time() < end:
            d = self.call("get", f"/task/task/{task_id}").get("data", {})
            st = d.get("state")
            if st in (4, 8):
                if d.get("num_err"):
                    raise FmgError(f"task {task_id}: {d.get('num_err')} fouten")
                return d
            if st in (3, 5, 6, 7):
                raise FmgError(f"task {task_id} state={st}: {d.get('title','')}")
            time.sleep(2)
        raise FmgError(f"task {task_id}: timeout")


def fmg_script_body(config):
    """FortiManager scripts run against the device DB: drop FortiOS-only batch wrapper."""
    return "\n".join(l for l in config.splitlines()
                     if l.strip() not in ("execute batch start", "execute batch end")) + "\n"


def pipeline(topo, configs, fmg, emit):
    """Yield-style pipeline; emit(step_id, status, msg). Raises on fatal error."""
    adom = topo["adom"]
    devices = [(d, "hub") for d in topo["hubs"]] + [(d, "edge") for d in topo["edges"]]

    emit("preflight", "running", "FortiManager bereikbaar? versie controleren")
    fmg.login()
    st = fmg.call("get", "/sys/status").get("data", {}) if not fmg.dry else {"Version": "dry-run"}
    ver = st.get("Version", "?")
    emit("preflight", "ok", f"verbonden, versie: {ver}")
    if not fmg.dry and "v8.0" not in ver:
        raise FmgError(f"FortiManager draait {ver}, verwacht 8.0.x")

    emit("adom", "running", f"ADOM '{adom}' aanmaken")
    if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}"):
        emit("adom", "ok", "ADOM bestaat al, hergebruikt")
    else:
        fmg.call("add", "/dvmdb/adom", [{"name": adom, "os_ver": topo["os_ver"], "mr": topo["mr"],
                                          "restricted_prds": ["fos"], "desc": "Created by fgt-sdwan-provision"}])
        emit("adom", "ok", f"ADOM '{adom}' aangemaakt (os {topo['os_ver']})")

    emit("devices", "running", f"{len(devices)} model devices toevoegen")
    for d, kind in devices:
        if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}/device/{d['name']}"):
            emit("devices", "running", f"{d['name']}: bestaat al")
            continue
        r = fmg.call("exec", "/dvm/cmd/add/device", {
            "adom": adom, "flags": ["create_task", "nonblocking"],
            "device": {"name": d["name"], "sn": d["serial"].strip(), "mgmt_mode": "fmg",
                       "os_type": "fos", "os_ver": topo["os_ver"], "mr": topo["mr"],
                       "platform_str": topo["platform"], "device action": "add_model",
                       "desc": f"SD-WAN {kind}"}})
        tid = (r.get("data") or {}).get("taskid") or (r.get("data") or {}).get("task")
        fmg.wait_task(tid)
        emit("devices", "running", f"{d['name']} ({d['serial']}) toegevoegd")
    emit("devices", "ok", "alle model devices aanwezig")

    emit("scripts", "running", "CLI-scripts (device database) aanmaken en uitvoeren")
    for d, kind in devices:
        name = f"{d['name']}-sdwan"
        body = fmg_script_body(configs[d["name"]])
        sdata = {"name": name, "type": "cli", "target": "device_database",
                 "desc": f"SD-WAN multi-VRF {kind}", "content": body}
        if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}/script/{name}"):
            fmg.call("set", f"/dvmdb/adom/{adom}/script/{name}", sdata)   # re-run: overwrite
        else:
            fmg.call("add", f"/dvmdb/adom/{adom}/script", [sdata])
        r = fmg.call("exec", f"/dvmdb/adom/{adom}/script/execute",
                     {"adom": adom, "script": name, "scope": [{"name": d["name"], "vdom": "root"}],
                      "package": None})
        tid = (r.get("data") or {}).get("task")
        fmg.wait_task(tid)
        emit("scripts", "running", f"{d['name']}: script uitgevoerd ({len(body.splitlines())} regels)")
    emit("scripts", "ok", "alle scripts uitgevoerd op de device database")

    emit("install", "running", "device settings installeren (alleen online devices)")
    r = fmg.call("exec", "/securityconsole/install/device",
                 {"adom": adom, "flags": ["none"],
                  "scope": [{"name": d["name"], "vdom": "root"} for d, _ in devices]})
    tid = (r.get("data") or {}).get("task")
    try:
        fmg.wait_task(tid)
        emit("install", "ok", "install voltooid")
    except FmgError as e:
        emit("install", "warn", f"install niet voltooid (devices nog offline?): {e}")
