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
            self.log("info", "dry run: login skipped")
            return
        res = self.call("exec", "sys/login/user", {"user": self.u, "passwd": self.p})
        self.session = res.get("_session")
        if not self.session:
            raise FmgError("login: no session received")

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
                    raise FmgError(f"task {task_id}: {d.get('num_err')} errors: {self._task_errors(task_id)}")
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
    """Poll FMG until every device is connected. With verbose=1 FortiManager answers enums as text ("up", "down",
    "UNKNOWN"); 1 is the numeric form. Checks at least once. Returns the names still offline."""
    if fmg.dry:
        return []
    end = time.time() + timeout
    pending = list(names)
    while pending:
        for n in list(pending):
            d = fmg.call("get", f"/dvmdb/adom/{adom}/device/{n}").get("data") or {}
            if d.get("conn_status") in (1, "up", "UP"):
                pending.remove(n)
                emit(step, "running", f"{n}: online in FortiManager")
        if not pending or time.time() >= end:
            break
        time.sleep(poll)
    return pending


def _bootstrap_fmg(topo, boot, emit):
    from . import bootstrap as bs
    f = topo["fmgr"]
    lines = bs.fmg_api_lines(boot.get("fmg_user")) + bs.fmg_network_lines(topo)   # address change stays last
    emit("bootstrap_fmg", "running", f"FortiManager {f['initial_ip']} -> {f['ip']} (port {f.get('port','port1')})")
    if boot["dry"]:
        for l in lines:
            emit("bootstrap_fmg", "running", f"[dry-run] {l}")
        emit("bootstrap_fmg", "running", "[dry-run] execute vm-license ********")
        emit("bootstrap_fmg", "ok", "dry run: nothing sent")
        return
    creds = {"user": boot["fmg_user"], "initial_pw": boot["fmg_initial_pw"], "new_pw": boot["fmg_new_pw"]}
    bs.bootstrap_device("fmg", f.get("name") or "fmg", lines, f["initial_ip"], bs.split_cidr(f["ip"])[0],
                        boot["fmg_token"], creds, lambda lvl, m: emit("bootstrap_fmg", "running", m),
                        connector=boot.get("connector"), waiter=boot.get("waiter") or bs.wait_tcp,
                        timing=boot.get("timing"))
    emit("bootstrap_fmg", "ok", "FortiManager licensed and on its static IP")


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
        raise FmgError("bootstrap failed for: " + "; ".join(errors))
    emit("bootstrap_fgt", "ok", "all FortiGates licensed and registered with FortiManager")


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
        emit(step, "ok", "dry run: nothing created on Proxmox")
        return
    pve = boot.get("pve_client") or pv.make_pve(topo["pve"], boot["pve_secret"])
    boot["pve_client"] = pve
    emit(step, "running", f"{len(specs)} VM(s) on Proxmox node {topo['pve']['node']}")
    pv.deploy_vms(pve, topo["pve"], specs, boot["images"], emit, step, state, vmids=boot.get("vmids"))
    emit(step, "ok", "VM(s) created and started")


def _fmg_routes(fmg, topo, emit, step="pve_wan"):
    """Routes on the FortiManager to the WAN emulator segments (also an existing FortiManager). Idempotent."""
    from .bootstrap import FMG_ROUTE_SEQ
    from .wanemu.net import fmg_routes
    want = fmg_routes(topo)
    if not want:
        emit(step, "running", "FortiManager is not in the uplink subnet: the lab router must route 100.127.200.0/22 to "
                              f"{topo['wanemu']['uplink']['ip'].split('/')[0]}")
        return
    port = topo["fmgr"].get("port", "port1")
    cli = "config system route / " + " / ".join(f"edit {n} / set device {port} / set dst {d} {m} / set gateway {g} / next"
                                               for n, (d, m, g) in enumerate(want, start=FMG_ROUTE_SEQ)) + " / end"
    try:
        have = fmg.call("get", "/cli/global/system/route").get("data") or []
        have = have if isinstance(have, list) else [have]
        dsts = {tuple(r.get("dst") or []) for r in have}
        used = {int(r.get("seq_num", 0)) for r in have if str(r.get("seq_num", "")).isdigit()}
        new, seq = [], FMG_ROUTE_SEQ
        for d, m, g in want:
            if (d, m) in dsts:
                continue
            while seq in used:
                seq += 1
            new.append({"seq_num": seq, "device": port, "dst": [d, m], "gateway": g})
            used.add(seq)
        if new:
            fmg.call("add", "/cli/global/system/route", new)
            emit(step, "running", "FortiManager: routes to " + ", ".join(r["dst"][0] for r in new) + f" via {want[0][2]}")
    except FmgError as e:
        emit(step, "warn", f"FortiManager routes not set ({e}). Set them by hand: {cli}")


def _wanemu(topo, boot, emit, step="pve_wan", fmg=None):
    """WAN emulator (always with Day-0 FortiGates): the LXC that routes ISP-HUB / ISP-A / ISP-B and does DHCP."""
    from . import provision_vms as pv
    from .wanemu import net, setup
    w = topo["wanemu"]
    if boot.get("dry"):
        for k, v in net.ct_params(topo, w.get("vmid") or 0, "ssh-ed25519 ...").items():
            if k.startswith("net"):
                emit(step, "running", f"[dry-run] {w['hostname']} {k}: {v}")
        for r in net.reservations(topo):
            emit(step, "running", f"[dry-run] DHCP {w['segments'][r['seg']]['name']}: {r['name']} {r['mac']} -> {r['ip']}")
        emit(step, "ok", "dry run: no container created")
        return
    pve = boot.get("pve_client") or pv.make_pve(topo["pve"], boot["pve_secret"])
    boot["pve_client"] = pve
    emit(step, "running", f"WAN emulator {w['hostname']} on Proxmox node {topo['pve']['node']}")
    try:
        r = setup.deploy_wanemu(pve, topo, boot["wanemu_secrets"], emit, step, connector=boot.get("wanemu_connector"),
                                waiter=boot.get("waiter"), vmids=boot.get("vmids"))
    except setup.WanEmuError as e:
        raise FmgError(str(e)) from e
    fmg = fmg or boot.get("fmg")
    if fmg is not None and not fmg.dry:
        _fmg_routes(fmg, topo, emit, step)
    segs = ", ".join(f"{g['name']} {g['subnet']}" for g in w["segments"].values())
    emit(step, "ok" if r["internet"] else "warn",       # no internet: keep the step on warning (licences will fail)
         f"{w['hostname']} (CT {r['vmid']}{', new' if r['created'] else ''}) ready: {segs}"
         + ("" if r["internet"] else " - but without internet from the segments"))


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
                progress(f"login attempt {n}/{attempts}: account temporarily locked, waiting 70 s")
                time.sleep(max(delay, 70))
                continue
            if code in (-22, -11):                            # login refused
                refused += 1
                if refused >= 2:
                    if code == -11:
                        raise FmgError(f"logging in through the FortiManager API is not allowed ({e}). The admin has no API access: "
                                       "run on the FortiManager: config system admin user / edit admin / set rpc-permit read-write / "
                                       "end (a new FortiManager has rpc-permit none for admin). Then deploy again.")
                    raise FmgError(f"the FortiManager refused the login ({e}). Check user name and password; "
                                   "repeated attempts with a wrong password lock the account for a while.")
            progress(f"login attempt {n}/{attempts}: FortiManager not ready yet ({str(e)[:160]}); retrying in {delay} s")
            time.sleep(delay)
    raise last


def _license_state(st):
    """(ok, text) from /sys/status of a FortiManager. Unknown fields count as ok: only a clear 'not licensed' waits."""
    lic = str(st.get("License Status") or st.get("license_status") or "").strip()
    sn = str(st.get("Serial Number") or st.get("serial") or "").strip()
    if lic and lic.lower() not in ("valid", "1"):
        return False, f"licence {lic}"
    if sn and re.fullmatch(r"FMG-?VM0+", sn, re.I):
        return False, f"serial number {sn} (not licensed yet)"
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
                st, ok, why = {}, False, f"status not readable ({str(e)[:120]})"
            else:
                ok, why = _license_state(st)
            if ok:
                return st
            progress(f"logged in, but {why}; waiting for the licence to become active (FortiManager reboots after the licence)")
            fmg.logout()
            fmg.session = None
            if time.time() > end:
                raise FmgError(f"FortiManager is still not licensed after {timeout} s ({why}). Check on the console "
                               "'get system status' and 'diagnose debug cloudinit show' (was the FortiFlex token applied?)")
            time.sleep(delay)
    finally:
        fmg.timeout = old_timeout


def osmr(topo):
    """FortiManager's own (os_ver, mr) notation: 7.6 is '7.0' MR6; 8.0 is '8.0' MR0."""
    v = topo["os_ver"]
    return ("7.0", 6) if v == "7.6" else (v, topo.get("mr", 0))


SCOPES = ("all", "fmg", "fgt", "tpl", "dev", "lan")   # lan = only the LAN client CTs on Proxmox
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


def install_devices(fmg, adom, configs, devices, emit, step="install", pending=()):
    """Per device a Quick install (device settings), then per role a Re-install policy of its package on the given
    devices of that role. For model devices ('Alleen model devices') this goes to the device database; for real
    FortiGates the caller passes only the online ones (pending = still offline, reported at the end). A failure is a
    warning for that device/package; the others continue."""
    failed, todo = [], {d["name"] for d, _ in devices}
    emit(step, "running", "Quick install (device settings) per model device")
    for d, _ in devices:
        try:
            r = fmg.call("exec", "/securityconsole/install/device",
                         {"adom": adom, "flags": ["none"], "scope": [{"name": d["name"], "vdom": "root"}]})
            fmg.wait_task((r.get("data") or {}).get("task"))
            emit(step, "running", f"{d['name']}: Quick install done")
        except FmgError as e:
            failed.append(d["name"])
            emit(step, "warn", f"{d['name']}: Quick install failed: {e}")
    emit(step, "running", "Re-install policy per role")
    for role in configs["roles"]:
        scope = [{"name": n, "vdom": "root"} for n in role["devices"] if n in todo]
        if not scope:
            continue
        try:
            r = fmg.call("exec", "/securityconsole/install/package",
                         {"adom": adom, "pkg": role["name"], "flags": ["none"], "scope": scope})
            fmg.wait_task((r.get("data") or {}).get("task"))
            emit(step, "running", f"policy package {role['name']} reinstalled on {', '.join(x['name'] for x in scope)}")
        except FmgError as e:
            failed.append(role["name"])
            emit(step, "warn", f"policy package {role['name']}: Re-install policy failed: {e}")
    if failed:
        emit(step, "warn", f"install finished with errors ({', '.join(failed)}); see the task in FortiManager for details")
    elif pending:
        emit(step, "warn", f"Quick install and Re-install policy done; still to do (offline): {', '.join(pending)}")
    else:
        emit(step, "ok", "Quick install and Re-install policy done for all devices")


def ensure_vm_ports(fmg, name, count, emit, step="devices"):
    """A FortiGate-VM model device only gets port1 in the FortiManager device database; add port2..portN (physical,
    vdom root) so templates and policies can use them. Idempotent: only missing ports are added (verified FMG 8.0.2)."""
    want = [f"port{i}" for i in range(1, count + 1)]
    if fmg.dry:
        emit(step, "running", f"[dry-run] {name}: ports {want[0]}-{want[-1]} in the device database")
        return
    url = f"/pm/config/device/{name}/global/system/interface"
    have = {i.get("name") for i in (fmg.call("get", url, fields=["name"]).get("data") or [])}
    missing = [p for p in want if p not in have]
    if missing:
        fmg.call("add", url, [{"name": p, "vdom": "root", "type": "physical"} for p in missing])
        emit(step, "running", f"{name}: ports {', '.join(missing)} created (FortiManager only creates port1 for a VM)")


def ensure_allow_vm(fmg, emit, step="devices"):
    """Allow FortiGate-VM (model) devices on this FortiManager. Only touches the setting when the FMG has it (7.4.7+)
    and it is not on yet; needed before adding FortiGate-VM model devices, also on an existing FortiManager."""
    url, key, val = ALLOW_VM
    if fmg.dry:
        emit(step, "running", f"[dry-run] {key} {val} (allow FortiGate-VM devices)")
        return
    cur = fmg.call("get", url).get("data") or {}
    if key not in cur or _same(cur[key], val):
        return
    try:
        fmg.call("set", url, {key: val})
    except FmgError as e:
        raise FmgError(f"FortiManager refuses FortiGate-VM devices and turning on {key} failed ({e}). Set it on the "
                       f"FortiManager: config system global / set {key} {val} / end")
    emit(step, "running", f"FortiManager: {key} turned on (FortiManager 7.4.7+ refuses FortiGate-VM model devices otherwise)")


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
        emit(step, "ok", "dry run: FortiAnalyzer features on (reboot) not done")
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
            emit(step, "running", f"{what}: set")
        else:
            emit(step, "running", f"{what}: already correct")
    url, key, val = FAZ
    if _same(cur(url).get(key), val):
        emit(step, "ok", "FortiManager settings in order (FortiAnalyzer features were already on)")
        return
    emit(step, "running", "turning on FortiAnalyzer features: the FortiManager reboots afterwards")
    try:
        fmg.call("set", url, {key: val})
    except FmgError as e:                                     # the reboot can cut the answer off
        emit(step, "running", f"connection lost while turning it on ({str(e)[:120]}), reboot expected")
    from . import bootstrap as bs
    waiter, (host, port) = boot.get("waiter") or bs.wait_tcp, _hostport(fmg)
    if waiter(host, port, False, boot.get("faz_down_timeout", 300)):
        emit(step, "running", "FortiManager is rebooting; waiting until it is back")
    else:
        emit(step, "warn", "no reboot seen within the wait time; continuing with login")
    if not waiter(host, port, True, boot.get("fmg_boot_timeout", 900)):
        raise FmgError(f"FortiManager does not come back on {host}:{port} after turning on FortiAnalyzer features")
    fmg.session = None
    st = wait_fmg_ready(fmg, boot, lambda m: emit(step, "running", m))
    got = (fmg.call("get", url).get("data") or {}).get(key)
    if not _same(got, val):
        raise FmgError(f"FortiAnalyzer features are still '{got}' after the reboot")
    emit(step, "ok", f"FortiManager settings in order; FortiAnalyzer features on (version {st.get('Version', '?')})")


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
        raise FmgError(f"unknown deployment choice '{scope}'")
    m_fmg = _method(boot.get("fmg")) if scope not in EXISTING_FMG else "none"
    m_fgt = _method(boot.get("fgt")) if scope not in NO_FGT else "none"
    why = {"fgt": "skipped (FortiGates only)", "fmg": "skipped (FortiManager only)",
           "tpl": "skipped (templates only)", "dev": "skipped (model devices only)"}
    why_fmg = why[scope] if scope in EXISTING_FMG else "skipped (not requested)"
    why_fgt = why[scope] if scope in NO_FGT else "skipped (not requested)"
    state = boot.setdefault("state", {})
    fgt_vm = scope != "lan" and _method(boot.get("fgt")) == "day0" and scope not in NO_FGT
    _plan_vmids(topo, boot, scope, scope != "lan" and _method(boot.get("fmg")) == "day0" and scope not in EXISTING_FMG,
                fgt_vm)
    _check_bridges(topo, boot, fgt_vm, fgt_vm or scope == "lan")
    if scope == "lan":                         # LAN clients only: no FortiManager, no FortiGates, no WAN emulator
        for s in ("pve_fmg", "bootstrap_fmg", "preflight", "fmg_settings", "adom", "devices", "templates", "pve_wan",
                  "pve_fgt", "bootstrap_fgt", "install"):
            emit(s, "skipped", "skipped (LAN clients only)")
        _clients(topo, boot, emit)
        return
    from . import bootstrap as bs
    from . import provision_vms as pv

    if m_fmg == "day0":
        _pve_vms(topo, boot, "fmg", "pve_fmg", emit, state)
        ip = bs.split_cidr(topo["fmgr"]["ip"])[0]
        emit("bootstrap_fmg", "running", f"waiting for FortiManager on {ip} (Day-0: IP + licence + reboot)")
        if boot.get("dry"):
            emit("bootstrap_fmg", "ok", "dry run: not waited")
        else:
            if not (boot.get("waiter") or bs.wait_tcp)(ip, 443, True, boot.get("fmg_boot_timeout", 900)):
                raise FmgError(f"FortiManager not reachable on {ip}:443. Open the console in Proxmox and check with "
                               "'get system interface' that port1 is mode static with this IP (a FortiManager VM can "
                               "be on DHCP with another address; then the Day-0 IP was not applied).")
            emit("bootstrap_fmg", "ok", f"FortiManager reachable on {ip}")
    else:
        emit("pve_fmg", "skipped", why_fmg)
        if m_fmg == "ssh":
            _bootstrap_fmg(topo, boot, emit)
        else:
            emit("bootstrap_fmg", "skipped", why_fmg)

    emit("preflight", "running", "FortiManager reachable? checking the version")
    if m_fmg == "day0" and not boot.get("dry"):
        st = wait_fmg_ready(fmg, boot, lambda m: emit("preflight", "running", m))   # FMG may still be rebooting
    else:
        try:
            fmg.login()
        except FmgError as e:
            if scope == "fgt":
                raise FmgError(f"{e} - 'FortiGates only' needs a running, reachable FortiManager "
                               "(deploy it first with 'FortiManager only' or 'Everything')")
            if scope in ("tpl", "dev"):
                raise FmgError(f"{e} - '{'Templates only' if scope == 'tpl' else 'Model devices only'}' needs a running, reachable FortiManager "
                               "(IP, admin user and password under FortiManager)")
            raise
        st = fmg.call("get", "/sys/status").get("data", {}) if not fmg.dry else {"Version": "dry-run"}
    ver = st.get("Version", "?")
    sn = str(st.get("Serial Number") or st.get("serial") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{8,32}", sn):
        # FortiOS 7.6+/8.0 treats a FortiManager whose serial is not set in central-management as 'unverified': the
        # model device is matched, but auto-link then times out (devupdfailed). The FortiGate Day-0 sets it.
        topo["fmgr"]["serial"] = sn
    emit("preflight", "ok", f"connected, version: {ver}" + (f", serial number {sn}" if topo["fmgr"].get("serial") else ""))
    if m_fmg == "day0" and not boot.get("dry"):
        pv.cleanup_isos(boot["pve_client"], topo["pve"], [topo["fmgr"].get("name") or "fmg"], state, emit, "bootstrap_fmg")
    if not fmg.dry and f"v{topo['os_ver']}" not in ver:
        raise FmgError(f"FortiManager runs {ver}, but the ADOM version is {topo['os_ver']}: choose the ADOM version of your FortiManager")

    if scope in EXISTING_FMG:
        emit("fmg_settings", "skipped", why[scope] + ": an existing FortiManager is not changed")
    else:
        emit("fmg_settings", "running", "idle timeouts, time zone, ADOM advanced mode and FortiAnalyzer features")
        apply_fmg_settings(fmg, boot, emit)

    emit("adom", "running", f"creating ADOM '{adom}'")
    if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}"):
        emit("adom", "ok", "ADOM exists, reused")
    else:
        fmg.call("add", "/dvmdb/adom", [{"name": adom, "os_ver": osmr(topo)[0], "mr": osmr(topo)[1],
                                          "restricted_prds": "fos", "desc": "Created by fgt-sdwan-provision"}])
        emit("adom", "ok", f"ADOM '{adom}' created (os {topo['os_ver']})")

    if scope in NO_DEVICES:
        emit("devices", "skipped", f"{why[scope]}: model devices come with 'FortiGates only'")
        emit("pve_wan", "skipped", why_fgt)
        emit("pve_fgt", "skipped", why_fgt)
        emit("bootstrap_fgt", "skipped", why_fgt)
        from .fmgapply import apply_plan
        emit("templates", "running", "creating templates, variables, policy packages and blueprints in the ADOM (without devices)")
        n = apply_plan(fmg, topo, configs, emit, devices=False)
        emit("templates", "ok", f"{n} FortiManager calls done; per-device values follow with 'FortiGates only'")
        emit("install", "skipped", f"{why[scope]}: then deploy the FortiGates with 'FortiGates only'")
        emit("clients", "skipped", why_fgt)
        return

    emit("devices", "running", f"adding {len(devices)} model devices")
    if "VM" in topo.get("platform", "").upper():
        ensure_allow_vm(fmg, emit)
    for d, kind in devices:
        if not fmg.dry and fmg.exists(f"/dvmdb/adom/{adom}/device/{d['name']}"):
            url = f"/dvmdb/adom/{adom}/device/{d['name']}"
            old = str((fmg.call("get", url).get("data") or {}).get("sn") or "").strip()
            new = d["serial"].strip()
            if old and new and old.upper() != new.upper():        # e.g. a new FortiFlex license for this device
                try:
                    fmg.call("update", url, {"sn": new})
                except FmgError as e:
                    raise FmgError(f"{d['name']}: the model device has serial number {old}, this deployment uses {new}, and "
                                   f"FortiManager refuses to change it ({e}). Delete model device {d['name']} in "
                                   f"ADOM {adom} and start the deployment again") from None
                emit("devices", "running", f"{d['name']}: exists, serial number {old} -> {new}")
            else:
                emit("devices", "running", f"{d['name']}: exists")
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
                raise FmgError(f"{d['name']}: FortiManager refuses FortiGate-VM devices ({e}). Set on the FortiManager: "
                               "config system global / set fgfm-allow-vm enable / end, and start the deployment again")
            raise
        emit("devices", "running", f"{d['name']} ({d['serial']}) added")
    if "VM" in topo.get("platform", "").upper():           # also for devices that already existed
        for d, _ in devices:
            ensure_vm_ports(fmg, d["name"], pv.fgt_port_count(d), emit)
    emit("devices", "ok", "all model devices present")

    # templates first: the model devices are complete before any real FortiGate registers against them
    from .fmgapply import apply_plan
    emit("templates", "running", "creating templates, variables, policy packages and blueprints in the ADOM")
    n = apply_plan(fmg, topo, configs, emit)
    emit("templates", "ok", f"{n} FortiManager calls done")

    # real FortiGates (Alles / Alleen FortiGates): VM + Day-0 (hub static wan1, licence, FMG link), register, online
    names = [d["name"] for d, _ in devices]
    if m_fgt == "day0":
        _wanemu(topo, boot, emit, fmg=fmg)
        _pve_vms(topo, boot, "fgt", "pve_fgt", emit, state)
        emit("bootstrap_fgt", "running", "waiting until the FortiGates are licensed and register with FortiManager")
        offline = wait_online(fmg, adom, names, boot.get("fgt_boot_timeout", 900), emit, "bootstrap_fgt",
                              boot.get("poll", 5))
        if offline:
            emit("bootstrap_fgt", "warn", f"not online yet: {', '.join(offline)} - check the console in Proxmox")
        else:
            emit("bootstrap_fgt", "ok", "all FortiGates online in FortiManager")
        if not boot.get("dry"):
            pv.cleanup_isos(boot["pve_client"], topo["pve"], [n for n in names if n not in offline], state, emit,
                            "bootstrap_fgt")
    else:
        emit("pve_wan", "skipped", why_fgt)
        emit("pve_fgt", "skipped", why_fgt)
        if m_fgt == "ssh":
            _bootstrap_fgts(topo, devices, boot, emit)
        else:
            emit("bootstrap_fgt", "skipped", why_fgt)

    # install: the same for model devices and real FortiGates (Quick install per device, then Re-install policy per
    # role); real FortiGates only once they are online
    if scope == "dev":
        install_devices(fmg, adom, configs, devices, emit)
        emit("clients", "skipped", why_fgt)
        return
    _install_online(fmg, adom, configs, devices, names, boot, emit, m_fgt)
    if m_fgt == "day0":
        _clients(topo, boot, emit)
    else:
        emit("clients", "skipped", why_fgt if m_fgt == "none" else "skipped (only with FortiGate VMs via Day-0)")


def _check_bridges(topo, boot, fgts, clients):
    """Before anything is created: every bridge this run uses exists and carries its VLANs (bridge-vids)."""
    from .wanemu import net
    if boot.get("dry") or not (fgts or clients) or not boot.get("pve_client"):
        return
    info = boot["pve_client"].bridge_info(topo["pve"]["node"])
    problems = net.bridge_problems(net.bridge_needs(topo, fgts, clients), info)
    if fgts or (topo.get("trafficgen") or {}).get("enabled"):
        problems += net.uplink_problems(topo, info)
    if problems:
        raise FmgError("Proxmox bridges: " + "; ".join(problems) + ". Choose the bridges under WAN emulation "
                       "(uplink bridge to the lab, bridge for the ISP segments and LAN VLANs) - nothing was created.")


def _plan_vmids(topo, boot, scope, fmg_vm, fgt_vm):
    """One block of consecutive VM ids for everything this run creates (FMG, wan-emu, hubs, spokes, clients)."""
    from . import provision_vms as pv
    names = pv.deploy_names(topo, scope, fmg_vm, fgt_vm)
    if boot.get("dry") or not names or "vmids" in boot:
        return
    pve = boot.get("pve_client") or pv.make_pve(topo["pve"], boot["pve_secret"])
    boot["pve_client"] = pve
    boot["vmids"] = pv.plan_vmids(pve, names, topo["pve"].get("first_vmid"), topo["pve"].get("name_prefix") or "")


def _clients(topo, boot, emit, step="clients"):
    """One LAN client CT per branch, on its LAN1 VLAN (after the install: the branch LAN exists now)."""
    from . import provision_vms as pv
    from .wanemu import net, setup
    if boot.get("dry"):
        for e in topo["edges"]:
            if not (e.get("lans") or [{}])[0].get("ip"):            # same rule as deploy_clients: no LAN, no client
                continue
            p = net.client_params(topo, e, 0, "")
            emit(step, "running", f"[dry-run] {p['hostname']}: {p['net0']}")
        emit(step, "ok", "dry run: no clients created")
        return
    pve = boot.get("pve_client") or pv.make_pve(topo["pve"], boot["pve_secret"])
    try:
        out = setup.deploy_clients(pve, topo, boot.get("client_pw") or "", emit, step, vmids=boot.get("vmids"))
    except Exception as e:                                    # a client is a convenience: never fail the deployment
        emit(step, "warn", f"LAN clients not (fully) created: {e}")
        return
    msg = f"{len(out)} LAN client(s); operate them from the Proxmox console (root, LAN client password)"
    from . import trafficgen
    if trafficgen.enabled(topo):
        try:
            r = trafficgen.deploy(pve, topo, boot["wanemu_secrets"]["ssh_key"], boot.get("tg_pw") or "", emit, step,
                                  connector=boot.get("wanemu_connector"), waiter=boot.get("waiter"), vmids=boot.get("vmids"))
            msg += f"; Traffic-Gen {'created' if r['created'] else 'updated'} (CT {r['vmid']}): {r['url']} (user admin)"
        except Exception as e:                                # optional as well: report, never fail the deployment
            emit(step, "warn", f"Traffic-Gen not installed: {e}")
            return
    emit(step, "ok", msg)


def _install_online(fmg, adom, configs, devices, names, boot, emit, m_fgt):
    emit("install", "running", "waiting until the FortiGates are online in FortiManager")
    wait = boot.get("online_timeout", 600 if m_fgt != "none" else 30)
    offline = wait_online(fmg, adom, names, wait, emit, "install", boot.get("poll", 5))
    if offline:
        emit("install", "warn", f"still offline after {wait}s: {', '.join(offline)} (install skipped for these devices)")
    online = [(d, k) for d, k in devices if d["name"] not in offline]
    if not online:
        emit("install", "warn", "no device online: install skipped; run the deployment again once they are online")
        return
    install_devices(fmg, adom, configs, online, emit, pending=offline)

