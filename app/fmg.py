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

    def _task_errors(self, task_id):
        try:
            lines = self.call("get", f"/task/task/{task_id}/line").get("data", [])
            return "; ".join(f"{l.get('name','')}: {l.get('detail','')}" for l in lines if l.get("err") or l.get("state") == "error")[:400]
        except FmgError:
            return ""

    def wait_task(self, task_id, timeout=600):
        if self.dry or not task_id:
            return {"state": 4, "percent": 100}
        end = time.time() + timeout
        while time.time() < end:
            d = self.call("get", f"/task/task/{task_id}").get("data", {})
            st = d.get("state")
            st = {"done": 4, "error": 5, "cancelled": 6, "cancelling": 6, "aborted": 7, "aborting": 7,
                  "warning": 8}.get(st, st) if isinstance(st, str) else st      # verbose=1 returns names
            if st in (4, 8):
                if d.get("num_err"):
                    raise FmgError(f"task {task_id}: {d.get('num_err')} fouten: {self._task_errors(task_id)}")
                return d
            if st in (3, 5, 6, 7):
                raise FmgError(f"task {task_id} state={st}: {d.get('title','')} {self._task_errors(task_id)}")
            time.sleep(2)
        raise FmgError(f"task {task_id}: timeout")


def fmg_script_body(config):
    """FortiManager scripts run against the device DB: drop FortiOS-only batch wrapper."""
    return "\n".join(l for l in config.splitlines()
                     if l.strip() not in ("execute batch start", "execute batch end")) + "\n"


def wait_online(fmg, adom, names, timeout, emit, step="install", poll=5):
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
                emit(step, "running", f"{n}: online in FortiManager")
        if pending:
            time.sleep(poll)
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


def _method(v):
    """bootstrap method per product: 'none' | 'ssh' (existing VM) | 'day0' (tool creates VM + cloud-init).
    Plain booleans from older callers mean 'ssh'."""
    if v is True:
        return "ssh"
    return v or "none"


def _pve_vms(topo, boot, which, step, emit, state):
    from . import provision_vms as pv
    specs = pv.build_specs(topo, boot.get("fgt_new_pw", ""), boot.get("fmg_new_pw", ""), boot.get("fgt_tokens", {}),
                           boot.get("fmg_token"), which)
    if boot.get("dry"):
        for sp in specs:
            emit(step, "running", "[dry-run] " + pv.plan_lines(sp))
        emit(step, "ok", "dry-run: niets aangemaakt op Proxmox")
        return
    pve = boot.get("pve_client") or pv.make_pve(topo["pve"], boot["pve_secret"])
    boot["pve_client"] = pve
    emit(step, "running", f"{len(specs)} VM('s) op Proxmox node {topo['pve']['node']}")
    pv.deploy_vms(pve, topo["pve"], specs, boot["images"], emit, step, state)
    emit(step, "ok", "VM('s) aangemaakt en gestart")


def _login_retry(fmg, attempts, delay):
    last = None
    for _ in range(attempts):
        try:
            fmg.login()
            return
        except FmgError as e:
            last = e
            time.sleep(delay)
    raise last


def osmr(topo):
    """FortiManager's own (os_ver, mr) notation: 7.6 is '7.0' MR6; 8.0 is '8.0' MR0."""
    v = topo["os_ver"]
    return ("7.0", 6) if v == "7.6" else (v, topo.get("mr", 0))


def pipeline(topo, configs, fmg, emit, boot=None):
    """boot (optional) = {'dry','fmg':bool,'fgt':bool, tokens/creds ...}. Raises on fatal error."""
    adom = topo["adom"]
    devices = [(d, "hub") for d in topo["hubs"]] + [(d, "edge") for d in topo["edges"]]
    boot = boot or {}
    m_fmg, m_fgt = _method(boot.get("fmg")), _method(boot.get("fgt"))
    state = boot.setdefault("state", {})
    from . import bootstrap as bs
    from . import provision_vms as pv

    if m_fmg == "day0":
        _pve_vms(topo, boot, "fmg", "pve_fmg", emit, state)
        ip = bs.split_cidr(topo["fmgr"]["ip"])[0]
        emit("bootstrap_fmg", "running", f"wachten op FortiManager op {ip} (Day-0: IP + licentie + herstart)")
        if boot.get("dry"):
            emit("bootstrap_fmg", "ok", "dry-run: niet gewacht")
        else:
            if not (boot.get("waiter") or bs.wait_tcp)(ip, 443, True, boot.get("fmg_boot_timeout", 900)):
                raise FmgError(f"FortiManager niet bereikbaar op {ip}:443. Controleer de console in Proxmox; "
                               "of de FMG de config-drive leest is niet bevestigd - gebruik anders methode 'ssh'.")
            emit("bootstrap_fmg", "ok", f"FortiManager bereikbaar op {ip}")
    else:
        emit("pve_fmg", "skipped", "overgeslagen (niet gevraagd)")
        if m_fmg == "ssh":
            _bootstrap_fmg(topo, boot, emit)
        else:
            emit("bootstrap_fmg", "skipped", "overgeslagen (niet gevraagd)")

    emit("preflight", "running", "FortiManager bereikbaar? versie controleren")
    if m_fmg == "day0" and not boot.get("dry"):
        _login_retry(fmg, boot.get("login_attempts", 20), boot.get("login_delay", 15))   # FMG may still be rebooting
    else:
        fmg.login()
    st = fmg.call("get", "/sys/status").get("data", {}) if not fmg.dry else {"Version": "dry-run"}
    ver = st.get("Version", "?")
    emit("preflight", "ok", f"verbonden, versie: {ver}")
    if m_fmg == "day0" and not boot.get("dry"):
        pv.cleanup_isos(boot["pve_client"], topo["pve"], [topo["fmgr"].get("name") or "fmg"], state, emit, "bootstrap_fmg")
    if not fmg.dry and f"v{topo['os_ver']}" not in ver:
        raise FmgError(f"FortiManager draait {ver}, maar de ADOM-versie is {topo['os_ver']}: kies de ADOM-versie van je FortiManager")

    emit("adom", "running", f"ADOM '{adom}' aanmaken")
    if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}"):
        emit("adom", "ok", "ADOM bestaat al, hergebruikt")
    else:
        fmg.call("add", "/dvmdb/adom", [{"name": adom, "os_ver": osmr(topo)[0], "mr": osmr(topo)[1],
                                          "restricted_prds": "fos", "desc": "Created by fgt-sdwan-provision"}])
        emit("adom", "ok", f"ADOM '{adom}' aangemaakt (os {topo['os_ver']})")

    emit("devices", "running", f"{len(devices)} model devices toevoegen")
    for d, kind in devices:
        if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}/device/{d['name']}"):
            emit("devices", "running", f"{d['name']}: bestaat al")
            continue
        r = fmg.call("exec", "/dvm/cmd/add/device", {
            "adom": adom, "flags": ["create_task", "nonblocking"],
            "device": {"name": d["name"], "sn": d["serial"].strip(), "mgmt_mode": "fmg",
                       "os_type": "fos", "os_ver": osmr(topo)[0], "mr": osmr(topo)[1],
                       "platform_str": topo["platform"], "device action": "add_model",
                       "desc": f"SD-WAN {kind}"}})
        tid = (r.get("data") or {}).get("taskid") or (r.get("data") or {}).get("task")
        fmg.wait_task(tid)
        emit("devices", "running", f"{d['name']} ({d['serial']}) toegevoegd")
    emit("devices", "ok", "alle model devices aanwezig")

    # FortiGates register against the model devices created above
    names = [d["name"] for d, _ in devices]
    if m_fgt == "day0":
        _pve_vms(topo, boot, "fgt", "pve_fgt", emit, state)
        emit("bootstrap_fgt", "running", "wachten tot de FortiGates gelicenseerd zijn en zich bij FortiManager melden")
        offline = wait_online(fmg, adom, names, boot.get("fgt_boot_timeout", 900), emit, "bootstrap_fgt",
                              boot.get("poll", 5))
        if offline:
            emit("bootstrap_fgt", "warn", f"nog niet online: {', '.join(offline)} - controleer de console in Proxmox")
        else:
            emit("bootstrap_fgt", "ok", "alle FortiGates online in FortiManager")
        if not boot.get("dry"):
            pv.cleanup_isos(boot["pve_client"], topo["pve"], [n for n in names if n not in offline], state, emit,
                            "bootstrap_fgt")
    else:
        emit("pve_fgt", "skipped", "overgeslagen (niet gevraagd)")
        if m_fgt == "ssh":
            _bootstrap_fgts(topo, devices, boot, emit)
        else:
            emit("bootstrap_fgt", "skipped", "overgeslagen (niet gevraagd)")

    from .fmgapply import apply_plan
    emit("templates", "running", "templates, variabelen, policy packages en blueprints in ADOM aanmaken")
    n = apply_plan(fmg, topo, configs, emit)
    emit("templates", "ok", f"{n} FortiManager-aanroepen uitgevoerd")

    emit("install", "running", "wachten tot de FortiGates online zijn in FortiManager")
    wait = boot.get("online_timeout", 600 if m_fgt != "none" else 30)
    offline = wait_online(fmg, adom, names, wait, emit, "install", boot.get("poll", 5))
    online = [d for d, _ in devices if d["name"] not in offline]
    if offline:
        emit("install", "warn", f"nog offline na {wait}s: {', '.join(offline)} (install wordt overgeslagen voor deze devices)")
    if not online:
        emit("install", "warn", "geen enkel device online: install overgeslagen; draai de uitrol opnieuw zodra ze online zijn")
        return
    for role in configs["roles"]:                      # policy package per role, template group via the device group
        tg = [{"name": d, "vdom": "root"} for d in role["devices"] if d not in offline]
        if not tg:
            continue
        r = fmg.call("exec", "/securityconsole/install/package",
                     {"adom": adom, "pkg": role["name"], "flags": ["none"], "scope": tg})
        try:
            fmg.wait_task((r.get("data") or {}).get("task"))
            emit("install", "running", f"policy package {role['name']} geinstalleerd op {', '.join(d['name'] for d in tg)}")
        except FmgError as e:
            emit("install", "warn", f"policy package {role['name']}: {e}")
    r = fmg.call("exec", "/securityconsole/install/device",
                 {"adom": adom, "flags": ["none"], "scope": [{"name": d["name"], "vdom": "root"} for d in online]})
    tid = (r.get("data") or {}).get("task")
    try:
        fmg.wait_task(tid)
        emit("install", "warn" if offline else "ok",
             "install voltooid" + (f" ({len(offline)} device(s) nog te doen)" if offline else ""))
    except FmgError as e:
        emit("install", "warn", f"install niet voltooid: {e}")
