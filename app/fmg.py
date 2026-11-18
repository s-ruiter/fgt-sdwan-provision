"""FortiManager JSON-RPC client and the deployment pipeline."""
import re
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
        self.timeout = 60

    def call(self, method, url, data=None, **extra):
        self.n += 1
        url = url if url.startswith("/") else "/" + url          # a real FortiManager answers -11 "No permission" without it
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
            r = requests.post(self.base, json=body, headers=headers, verify=False, timeout=self.timeout)
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
    lines = bs.fmg_api_lines(boot.get("fmg_user")) + bs.fmg_network_lines(topo)   # address change stays last
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


def _login_retry(fmg, attempts, delay, progress=None):
    """A FortiManager that just booted is not ready for a while, so connection problems are retried. A refused login is
    not: repeating a wrong password locks the account (default: 3 failures), so that stops after two tries with a clear
    message, and an active lockout (-23) is waited out instead of being hammered.
    progress(msg) (optional) reports every failed attempt, so a long wait is visible in the job log."""
    last, refused = None, 0
    progress = progress or (lambda _m: None)
    for n in range(1, attempts + 1):
        try:
            fmg.login()
            return
        except FmgError as e:
            last = e
            m = re.search(r"\((-?\d+)\)", str(e))
            code = int(m.group(1)) if m else None
            if code == -23:                                   # lockout / login limit: wait it out (default lockout 60 s)
                progress(f"inlogpoging {n}/{attempts}: account tijdelijk geblokkeerd, 70 s wachten")
                time.sleep(max(delay, 70))
                continue
            if code in (-22, -11):                            # login refused
                refused += 1
                if refused >= 2:
                    if code == -11:
                        raise FmgError(f"inloggen via de FortiManager-API is niet toegestaan ({e}). De admin heeft geen API-toegang: "
                                       "voer op de FortiManager uit: config system admin user / edit admin / set rpc-permit read-write / "
                                       "end (een nieuwe FortiManager heeft rpc-permit none voor admin). Daarna opnieuw uitrollen.")
                    raise FmgError(f"inloggen op de FortiManager is geweigerd ({e}). Controleer gebruikersnaam en wachtwoord; "
                                   "herhaald proberen met een fout wachtwoord blokkeert het account tijdelijk.")
            progress(f"inlogpoging {n}/{attempts}: FortiManager nog niet klaar ({str(e)[:160]}); opnieuw over {delay} s")
            time.sleep(delay)
    raise last


def _license_state(st):
    """(ok, text) from /sys/status of a FortiManager. Unknown fields count as ok: only a clear 'not licensed' waits."""
    lic = str(st.get("License Status") or st.get("license_status") or "").strip()
    sn = str(st.get("Serial Number") or st.get("serial") or "").strip()
    if lic and lic.lower() not in ("valid", "1"):
        return False, f"licentie {lic}"
    if sn and re.fullmatch(r"FMG-?VM0+", sn, re.I):
        return False, f"serienummer {sn} (nog ongelicenseerd)"
    return True, lic or "?"


def wait_fmg_ready(fmg, boot, progress):
    """Day-0: log in once the FortiManager answers and wait until its FortiFlex licence is active.
    After the licence the FMG reboots and initialises its database; port 443 can already be open before that reboot,
    so a login alone is not enough. Returns /sys/status data."""
    attempts, delay = boot.get("login_attempts", 60), boot.get("login_delay", 15)
    timeout = boot.get("fmg_ready_timeout", 1800)
    end = time.time() + timeout
    old_timeout, fmg.timeout = fmg.timeout, 20             # a booting FMG can accept TCP and never answer
    try:
        while True:
            _login_retry(fmg, attempts, delay, progress)
            try:
                st = fmg.call("get", "/sys/status").get("data", {}) or {}
            except FmgError as e:
                st, ok, why = {}, False, f"status niet leesbaar ({str(e)[:120]})"
            else:
                ok, why = _license_state(st)
            if ok:
                return st
            progress(f"ingelogd, maar {why}; wachten tot de licentie actief is (FortiManager herstart na de licentie)")
            fmg.logout()
            fmg.session = None
            if time.time() > end:
                raise FmgError(f"FortiManager is na {timeout} s nog niet gelicenseerd ({why}). Controleer op de console "
                               "'get system status' en 'diagnose debug cloudinit show' (is de FortiFlex-token toegepast?)")
            time.sleep(delay)
    finally:
        fmg.timeout = old_timeout


def osmr(topo):
    """FortiManager's own (os_ver, mr) notation: 7.6 is '7.0' MR6; 8.0 is '8.0' MR0."""
    v = topo["os_ver"]
    return ("7.0", 6) if v == "7.6" else (v, topo.get("mr", 0))


SCOPES = ("all", "fmg", "fgt", "tpl", "dev")
NO_DEVICES = ("fmg", "tpl")           # scopes that create no model devices (templates only)
EXISTING_FMG = ("fgt", "tpl", "dev")  # scopes against an existing FortiManager (no FMG VM, settings untouched)
NO_FGT = ("fmg", "tpl", "dev")        # scopes that create no FortiGate VMs and do no install

# FortiManager system settings the tool always sets (names/values checked on a real FMG 8.0.2 via `option: syntax`).
# The idle timeouts are in SECONDS on FMG 8.0 (factory 900 = 15 min). timezone is the numeric FortiOS code:
# 28 = (GMT+1:00) Amsterdam, Berlin, Bern, Rome, Stockholm, Vienna.
IDLE_TIMEOUT_S = 180 * 60
FMG_SETTINGS = [
    ("/cli/global/system/admin/setting", "uitlogtimers 180 min",
     {"idle_timeout": IDLE_TIMEOUT_S, "idle_timeout_gui": IDLE_TIMEOUT_S, "idle_timeout_api": IDLE_TIMEOUT_S,
      "idle_timeout_sso": IDLE_TIMEOUT_S}),
    ("/cli/global/system/global", "tijdzone Europe/Amsterdam", {"timezone": "28"}),
    ("/cli/global/system/global", "ADOMs aan, advanced mode", {"adom-status": "enable", "adom-mode": "advanced"}),
]
FAZ = ("/cli/global/system/global", "faz-status", "enable")
# FortiManager 7.4.7+ refuses VM devices by default, model devices included ("VM device is not allowed to add").
ALLOW_VM = ("/cli/global/system/global", "fgfm-allow-vm", "enable")


def install_model_devices(fmg, adom, configs, devices, emit, step="install"):
    """'Alleen model devices': per device a Quick install (device settings only, to the device database: there is no
    real FortiGate yet), then per role a Re-install policy of its package on the devices of that role. Does not wait
    for devices to come online. A failure is a warning for that device/package; the others continue."""
    failed = []
    emit(step, "running", "Quick install (device settings) per model device")
    for d, _ in devices:
        try:
            r = fmg.call("exec", "/securityconsole/install/device",
                         {"adom": adom, "flags": ["none"], "scope": [{"name": d["name"], "vdom": "root"}]})
            fmg.wait_task((r.get("data") or {}).get("task"))
            emit(step, "running", f"{d['name']}: Quick install voltooid")
        except FmgError as e:
            failed.append(d["name"])
            emit(step, "warn", f"{d['name']}: Quick install mislukt: {e}")
    emit(step, "running", "Re-install policy per rol")
    for role in configs["roles"]:
        scope = [{"name": n, "vdom": "root"} for n in role["devices"]]
        try:
            r = fmg.call("exec", "/securityconsole/install/package",
                         {"adom": adom, "pkg": role["name"], "flags": ["none"], "scope": scope})
            fmg.wait_task((r.get("data") or {}).get("task"))
            emit(step, "running", f"policy package {role['name']} opnieuw geinstalleerd op {', '.join(role['devices'])}")
        except FmgError as e:
            failed.append(role["name"])
            emit(step, "warn", f"policy package {role['name']}: Re-install policy mislukt: {e}")
    if failed:
        emit(step, "warn", f"install klaar met fouten ({', '.join(failed)}); zie de taak in FortiManager voor details")
    else:
        emit(step, "ok", "Quick install en Re-install policy voltooid voor alle model devices")


def ensure_vm_ports(fmg, name, count, emit, step="devices"):
    """A FortiGate-VM model device only gets port1 in the FortiManager device database; add port2..portN (physical,
    vdom root) so templates and policies can use them. Idempotent: only missing ports are added (verified FMG 8.0.2)."""
    want = [f"port{i}" for i in range(1, count + 1)]
    if fmg.dry:
        emit(step, "running", f"[dry-run] {name}: poorten {want[0]}-{want[-1]} in de device-database")
        return
    url = f"/pm/config/device/{name}/global/system/interface"
    have = {i.get("name") for i in (fmg.call("get", url, fields=["name"]).get("data") or [])}
    missing = [p for p in want if p not in have]
    if missing:
        fmg.call("add", url, [{"name": p, "vdom": "root", "type": "physical"} for p in missing])
        emit(step, "running", f"{name}: poorten {', '.join(missing)} aangemaakt (FortiManager maakt bij een VM alleen port1)")


def ensure_allow_vm(fmg, emit, step="devices"):
    """Allow FortiGate-VM (model) devices on this FortiManager. Only touches the setting when the FMG has it (7.4.7+)
    and it is not on yet; needed before adding FortiGate-VM model devices, also on an existing FortiManager."""
    url, key, val = ALLOW_VM
    if fmg.dry:
        emit(step, "running", f"[dry-run] {key} {val} (FortiGate-VM devices toestaan)")
        return
    cur = fmg.call("get", url).get("data") or {}
    if key not in cur or _same(cur[key], val):
        return
    try:
        fmg.call("set", url, {key: val})
    except FmgError as e:
        raise FmgError(f"FortiManager weigert FortiGate-VM devices en {key} aanzetten lukte niet ({e}). Zet het op de "
                       f"FortiManager: config system global / set {key} {val} / end")
    emit(step, "running", f"FortiManager: {key} aangezet (FortiManager 7.4.7+ weigert anders FortiGate-VM model devices)")


def _same(cur, want):
    return str(cur).strip().lower() == str(want).strip().lower()


def _hostport(fmg):
    from urllib.parse import urlparse
    u = urlparse(fmg.base)
    return u.hostname, u.port or 443


def apply_fmg_settings(fmg, boot, emit, step="fmg_settings"):
    """Idempotent: read each setting, only set what differs. FortiAnalyzer features go last: enabling them reboots the
    FortiManager, so the tool waits for the reboot and logs in again."""
    if fmg.dry:
        for _, what, vals in FMG_SETTINGS:
            emit(step, "running", f"[dry-run] {what}: {vals}")
        emit(step, "ok", "dry-run: FortiAnalyzer-features aan (herstart) niet uitgevoerd")
        return
    cache = {}

    def cur(url):
        if url not in cache:
            cache[url] = fmg.call("get", url).get("data") or {}
        return cache[url]

    for url, what, vals in FMG_SETTINGS:
        diff = {k: v for k, v in vals.items() if not _same(cur(url).get(k), v)}
        if diff:
            fmg.call("set", url, diff)
            cur(url).update(diff)
            emit(step, "running", f"{what}: ingesteld")
        else:
            emit(step, "running", f"{what}: stond al goed")
    url, key, val = FAZ
    if _same(cur(url).get(key), val):
        emit(step, "ok", "FortiManager-instellingen in orde (FortiAnalyzer-features stonden al aan)")
        return
    emit(step, "running", "FortiAnalyzer-features aanzetten: de FortiManager herstart daarna")
    try:
        fmg.call("set", url, {key: val})
    except FmgError as e:                                     # the reboot can cut the answer off
        emit(step, "running", f"verbinding verbroken tijdens aanzetten ({str(e)[:120]}), herstart verwacht")
    from . import bootstrap as bs
    waiter, (host, port) = boot.get("waiter") or bs.wait_tcp, _hostport(fmg)
    if waiter(host, port, False, boot.get("faz_down_timeout", 300)):
        emit(step, "running", "FortiManager herstart; wachten tot hij terug is")
    else:
        emit(step, "warn", "geen herstart gezien binnen de wachttijd; verder met inloggen")
    if not waiter(host, port, True, boot.get("fmg_boot_timeout", 900)):
        raise FmgError(f"FortiManager komt na het aanzetten van FortiAnalyzer-features niet terug op {host}:{port}")
    fmg.session = None
    st = wait_fmg_ready(fmg, boot, lambda m: emit(step, "running", m))
    got = (fmg.call("get", url).get("data") or {}).get(key)
    if not _same(got, val):
        raise FmgError(f"FortiAnalyzer-features staan na de herstart nog op '{got}'")
    emit(step, "ok", f"FortiManager-instellingen in orde; FortiAnalyzer-features aan (versie {st.get('Version', '?')})")


def pipeline(topo, configs, fmg, emit, boot=None):
    """boot (optional) = {'dry','fmg','fgt' (method), 'scope', tokens/creds ...}. Raises on fatal error.

    scope: 'all' (default), 'fmg' (only the FortiManager: VM, ZTP, ADOM, templates; no model devices, no FortiGates,
    no install), 'fgt' (only the FortiGates against an existing FortiManager: no FMG VM/ZTP) or 'tpl' (only ADOM +
    templates on an existing FortiManager: no VMs, no FMG system settings, no model devices, no install) or 'dev'
    (only ADOM + model devices + templates with the per-device values on an existing FortiManager: no VMs, no link to
    real FortiGates, no install)."""
    adom = topo["adom"]
    devices = [(d, "hub") for d in topo["hubs"]] + [(d, "edge") for d in topo["edges"]]
    boot = boot or {}
    scope = boot.get("scope") or "all"
    if scope not in SCOPES:
        raise FmgError(f"onbekende uitrol-keuze '{scope}'")
    m_fmg = _method(boot.get("fmg")) if scope not in EXISTING_FMG else "none"
    m_fgt = _method(boot.get("fgt")) if scope not in NO_FGT else "none"
    why = {"fgt": "overgeslagen (alleen FortiGates)", "fmg": "overgeslagen (alleen FortiManager)",
           "tpl": "overgeslagen (alleen templates)", "dev": "overgeslagen (alleen model devices)"}
    why_fmg = why[scope] if scope in EXISTING_FMG else "overgeslagen (niet gevraagd)"
    why_fgt = why[scope] if scope in NO_FGT else "overgeslagen (niet gevraagd)"
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
                raise FmgError(f"FortiManager niet bereikbaar op {ip}:443. Open de console in Proxmox en controleer met "
                               "'get system interface' of port1 op mode static staat met dit IP (een FortiManager-VM kan "
                               "op DHCP staan en dan een ander adres hebben; het Day-0 IP is dan niet overgenomen).")
            emit("bootstrap_fmg", "ok", f"FortiManager bereikbaar op {ip}")
    else:
        emit("pve_fmg", "skipped", why_fmg)
        if m_fmg == "ssh":
            _bootstrap_fmg(topo, boot, emit)
        else:
            emit("bootstrap_fmg", "skipped", why_fmg)

    emit("preflight", "running", "FortiManager bereikbaar? versie controleren")
    if m_fmg == "day0" and not boot.get("dry"):
        st = wait_fmg_ready(fmg, boot, lambda m: emit("preflight", "running", m))   # FMG may still be rebooting
    else:
        try:
            fmg.login()
        except FmgError as e:
            if scope == "fgt":
                raise FmgError(f"{e} - 'Alleen FortiGates' heeft een draaiende, bereikbare FortiManager nodig "
                               "(rol die eerst uit met 'Alleen FortiManager' of 'Alles')")
            if scope in ("tpl", "dev"):
                raise FmgError(f"{e} - '{'Alleen templates' if scope == 'tpl' else 'Alleen model devices'}' heeft een draaiende, bereikbare FortiManager nodig "
                               "(IP, admin-gebruiker en wachtwoord bij FortiManager)")
            raise
        st = fmg.call("get", "/sys/status").get("data", {}) if not fmg.dry else {"Version": "dry-run"}
    ver = st.get("Version", "?")
    emit("preflight", "ok", f"verbonden, versie: {ver}")
    if m_fmg == "day0" and not boot.get("dry"):
        pv.cleanup_isos(boot["pve_client"], topo["pve"], [topo["fmgr"].get("name") or "fmg"], state, emit, "bootstrap_fmg")
    if not fmg.dry and f"v{topo['os_ver']}" not in ver:
        raise FmgError(f"FortiManager draait {ver}, maar de ADOM-versie is {topo['os_ver']}: kies de ADOM-versie van je FortiManager")

    if scope in EXISTING_FMG:
        emit("fmg_settings", "skipped", why[scope] + ": een bestaande FortiManager wordt niet aangepast")
    else:
        emit("fmg_settings", "running", "uitlogtimers, tijdzone, ADOM advanced mode en FortiAnalyzer-features")
        apply_fmg_settings(fmg, boot, emit)

    emit("adom", "running", f"ADOM '{adom}' aanmaken")
    if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}"):
        emit("adom", "ok", "ADOM bestaat al, hergebruikt")
    else:
        fmg.call("add", "/dvmdb/adom", [{"name": adom, "os_ver": osmr(topo)[0], "mr": osmr(topo)[1],
                                          "restricted_prds": "fos", "desc": "Created by fgt-sdwan-provision"}])
        emit("adom", "ok", f"ADOM '{adom}' aangemaakt (os {topo['os_ver']})")

    if scope in NO_DEVICES:
        emit("devices", "skipped", f"{why[scope]}: model devices komen bij 'Alleen FortiGates'")
        emit("pve_fgt", "skipped", why_fgt)
        emit("bootstrap_fgt", "skipped", why_fgt)
        from .fmgapply import apply_plan
        emit("templates", "running", "templates, variabelen, policy packages en blueprints in ADOM aanmaken (zonder devices)")
        n = apply_plan(fmg, topo, configs, emit, devices=False)
        emit("templates", "ok", f"{n} FortiManager-aanroepen uitgevoerd; waarden per device volgen bij 'Alleen FortiGates'")
        emit("install", "skipped", f"{why[scope]}: rol daarna de FortiGates uit met 'Alleen FortiGates'")
        return

    emit("devices", "running", f"{len(devices)} model devices toevoegen")
    if "VM" in topo.get("platform", "").upper():
        ensure_allow_vm(fmg, emit)
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
        try:
            fmg.wait_task(tid)
        except FmgError as e:
            if "vm device is not allowed" in str(e).lower():
                raise FmgError(f"{d['name']}: FortiManager weigert FortiGate-VM devices ({e}). Zet op de FortiManager: "
                               "config system global / set fgfm-allow-vm enable / end, en start de uitrol opnieuw")
            raise
        emit("devices", "running", f"{d['name']} ({d['serial']}) toegevoegd")
    if "VM" in topo.get("platform", "").upper():           # also for devices that already existed
        for d, _ in devices:
            ensure_vm_ports(fmg, d["name"], pv.fgt_port_count(d), emit)
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
        emit("pve_fgt", "skipped", why_fgt)
        if m_fgt == "ssh":
            _bootstrap_fgts(topo, devices, boot, emit)
        else:
            emit("bootstrap_fgt", "skipped", why_fgt)

    from .fmgapply import apply_plan
    emit("templates", "running", "templates, variabelen, policy packages en blueprints in ADOM aanmaken")
    n = apply_plan(fmg, topo, configs, emit)
    emit("templates", "ok", f"{n} FortiManager-aanroepen uitgevoerd")

    if scope == "dev":
        install_model_devices(fmg, adom, configs, devices, emit)
        return

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
