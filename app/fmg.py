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


def wait_online(fmg, adom, names, timeout, emit):
    """Poll FMG until every device reports conn_status == 1 (up). Returns list of still-offline names."""
    if fmg.dry:
        return []
    end = time.time() + timeout
    pending = list(names)
    while pending and time.time() < end:
        for n in list(pending):
            d = fmg.call("get", f"/dvmdb/adom/{adom}/device/{n}").get("data") or {}
            if d.get("conn_status") == 1:
                pending.remove(n)
                emit("install", "running", f"{n}: online in FortiManager")
        if pending:
            time.sleep(5)
    return pending


def _bootstrap_fmg(topo, boot, emit):
    from . import bootstrap as bs
    f = topo["fmgr"]
    lines = bs.fmg_network_lines(topo)
    emit("bootstrap_fmg", "running", f"FortiManager {f['initial_ip']} -> {f['ip']} (poort {f.get('port','port1')})")
    if boot["dry"]:
        for l in lines:
            emit("bootstrap_fmg", "running", f"[dry-run] {l}")
        emit("bootstrap_fmg", "running", "[dry-run] execute vm-license ********")
        emit("bootstrap_fmg", "ok", "dry-run: niets verstuurd")
        return
    creds = {"user": boot["fmg_user"], "initial_pw": boot["fmg_initial_pw"], "new_pw": boot["fmg_new_pw"]}
    bs.bootstrap_device("fmg", f.get("name") or "fmg", lines, f["initial_ip"], bs.split_cidr(f["ip"])[0],
                        boot["fmg_token"], creds, lambda lvl, m: emit("bootstrap_fmg", "running", m),
                        connector=boot.get("connector"), waiter=boot.get("waiter") or bs.wait_tcp,
                        timing=boot.get("timing"))
    emit("bootstrap_fmg", "ok", "FortiManager gelicenseerd en op vast IP")


def _bootstrap_fgts(topo, devices, boot, emit):
    from concurrent.futures import ThreadPoolExecutor
    from . import bootstrap as bs
    errors = []

    def one(d):
        name = d["name"]
        lines = bs.fgt_network_lines(d, topo)
        target = bs.split_cidr(d["wan1_ip"])[0] if d.get("wan1_mode") == "static" else d["initial_ip"]
        log = lambda lvl, m: emit("bootstrap_fgt", "running", m)
        if boot["dry"]:
            for l in lines:
                log("info", f"[dry-run] {name}: {l}")
            log("info", f"[dry-run] {name}: execute vm-license ********")
            return
        creds = {"user": boot["fgt_user"], "initial_pw": boot["fgt_initial_pw"], "new_pw": boot["fgt_new_pw"]}
        bs.bootstrap_device("fgt", name, lines, d["initial_ip"], target, boot["fgt_tokens"][name], creds, log,
                            connector=boot.get("connector"), waiter=boot.get("waiter") or bs.wait_tcp,
                            timing=boot.get("timing"), expect_serial=d["serial"])

    emit("bootstrap_fgt", "running", f"{len(devices)} FortiGates: wan1, DNS, central-management, FortiFlex")
    with ThreadPoolExecutor(max_workers=len(devices)) as ex:
        futs = {ex.submit(one, d): d["name"] for d, _ in devices}
        for fut, name in futs.items():
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 - collect per-device failures
                errors.append(f"{name}: {e}")
                emit("bootstrap_fgt", "error", f"{name}: {e}")
    if errors:
        raise FmgError("bootstrap mislukt voor: " + "; ".join(errors))
    emit("bootstrap_fgt", "ok", "alle FortiGates gelicenseerd en geregistreerd bij FortiManager")


def pipeline(topo, configs, fmg, emit, boot=None):
    """boot (optional) = {'dry','fmg':bool,'fgt':bool, tokens/creds ...}. Raises on fatal error."""
    adom = topo["adom"]
    devices = [(d, "hub") for d in topo["hubs"]] + [(d, "edge") for d in topo["edges"]]
    boot = boot or {}

    if boot.get("fmg"):
        _bootstrap_fmg(topo, boot, emit)
    else:
        emit("bootstrap_fmg", "skipped", "overgeslagen (niet gevraagd)")

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

    # FortiGates register against the model devices created above
    if boot.get("fgt"):
        _bootstrap_fgts(topo, devices, boot, emit)
    else:
        emit("bootstrap_fgt", "skipped", "overgeslagen (niet gevraagd)")

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

    emit("install", "running", "wachten tot de FortiGates online zijn in FortiManager")
    wait = boot.get("online_timeout", 600 if boot.get("fgt") else 30)
    offline = wait_online(fmg, adom, [d["name"] for d, _ in devices], wait, emit)
    online = [d for d, _ in devices if d["name"] not in offline]
    if offline:
        emit("install", "warn", f"nog offline na {wait}s: {', '.join(offline)} (install wordt overgeslagen voor deze devices)")
    if not online:
        emit("install", "warn", "geen enkel device online: install overgeslagen; draai de uitrol opnieuw zodra ze online zijn")
        return
    r = fmg.call("exec", "/securityconsole/install/device",
                 {"adom": adom, "flags": ["none"], "scope": [{"name": d["name"], "vdom": "root"} for d in online]})
    tid = (r.get("data") or {}).get("task")
    try:
        fmg.wait_task(tid)
        emit("install", "warn" if offline else "ok",
             "install voltooid" + (f" ({len(offline)} device(s) nog te doen)" if offline else ""))
    except FmgError as e:
        emit("install", "warn", f"install niet voltooid: {e}")
