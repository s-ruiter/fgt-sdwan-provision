"""ZTP bootstrap over SSH: wan1 addressing, DNS, central-management and FortiFlex licensing.

Runs from the deploy container against factory-fresh FortiGate-VMs / a FortiManager-VM:
  1. log in on the device's initial address (forced password change is handled),
  2. configure port1 (static or DHCP), DNS and - FortiGate only - central-management,
  3. apply the FortiFlex token with `execute vm-license <token>` (the device reboots),
  4. wait for the device to come back and verify it.

Tokens/passwords are never logged: every outgoing line is masked through `Masker`.
"""
import re
import socket
import time

import paramiko

from .model import split_cidr

PROMPT = re.compile(r"(?m)^\S+(?: \([^)\n]*\))? # ?$")
ERR = re.compile(r"(?i)command fail|unknown action|parse error|return code -\d+|invalid|not found")


class BootstrapError(Exception):
    pass


class Timing:
    def __init__(self, cmd=30, login=30, reboot_down=90, reboot_up=480, link_up=90):
        self.cmd, self.login = cmd, login
        self.reboot_down, self.reboot_up, self.link_up = reboot_down, reboot_up, link_up


class Masker:
    def __init__(self, *secrets):
        self.secrets = [s for s in secrets if s]

    def add(self, *secrets):
        self.secrets += [s for s in secrets if s]

    def __call__(self, text):
        for s in self.secrets:
            text = text.replace(s, "********")
        return text


# --------------------------------------------------------------------------- CLI builders
def fgt_network_lines(dev, topo):
    """Config that gives wan1 an address and points the FortiGate at FortiManager.
    The interface address change is LAST: it drops the session when the IP moves."""
    port = dev["wan1_intf"]
    fmg_ip = split_cidr(topo["fmgr"]["ip"])[0]
    access = topo.get("wan1_access", "ping https ssh fgfm")
    lines = ["config system dns", f"set primary {topo['dns_primary']}"]
    if topo.get("dns_secondary"):
        lines.append(f"set secondary {topo['dns_secondary']}")
    lines += ["end",
              "config system central-management", "set type fortimanager", f'set fmg "{fmg_ip}"', "end"]
    if dev.get("wan1_mode") == "static":
        ip, mask = split_cidr(dev["wan1_ip"])
        lines += ["config router static", "edit 1", f"set gateway {dev['wan1_gw'].strip()}",
                  f'set device "{port}"', "next", "end",
                  "config system interface", f'edit "{port}"', "set mode static", f"set allowaccess {access}",
                  f"set ip {ip} {mask}", "next", "end"]
    else:
        lines += ["config system interface", f'edit "{port}"', "set mode dhcp",
                  f"set allowaccess {access}", "next", "end"]
    return lines


def fmg_network_lines(topo, ip_last=True):
    """FortiManager hostname, DNS, port1 and default route.
    port1 is put in `mode static` explicitly: a new FortiManager VM can come up with DHCP on port1, and `set ip` alone
    then leaves it on the DHCP address. ip_last=True (SSH): the address change drops the session, so it goes last;
    ip_last=False (Day-0 config-drive): interface first, so the gateway is already inside the port's subnet."""
    f = topo["fmgr"]
    ip, mask = split_cidr(f["ip"])
    port = f.get("port", "port1")
    lines = []
    if f.get("name"):
        lines += ["config system global", f"set hostname {f['name']}", "end"]
    lines += ["config system dns", f"set primary {topo['dns_primary']}"]
    if topo.get("dns_secondary"):
        lines.append(f"set secondary {topo['dns_secondary']}")
    lines.append("end")
    route = ["config system route", "edit 1", f'set device "{port}"', f"set gateway {f['gateway'].strip()}", "next", "end"]
    iface = ["config system interface", f'edit "{port}"', "set allowaccess ping https ssh", "set mode static",
             f"set ip {ip} {mask}", "next", "end"]
    return lines + (route + iface if ip_last else iface + route)


def fmg_api_lines(user="admin"):
    """JSON-RPC access for the admin the tool logs in with: a new FortiManager has rpc-permit none for `admin`,
    and the API login then answers -11 "No permission for the resource"."""
    return ["config system admin user", f'edit "{user or "admin"}"', "set rpc-permit read-write", "next", "end"]


def license_line(token):
    return f"execute vm-license {token.strip()}"


# --------------------------------------------------------------------------- shell
class Shell:
    """Minimal expect-style wrapper around a paramiko interactive channel."""

    def __init__(self, chan, mask, log, client=None):
        self.chan, self.mask, self.log, self.client = chan, mask, log, client
        self.buf = ""

    def _pump(self, wait=0.2):
        got = False
        end = time.time() + wait
        while time.time() < end:
            if self.chan.recv_ready():
                self.buf += self.chan.recv(65535).decode("utf-8", "replace")
                got = True
                end = time.time() + 0.05
            else:
                time.sleep(0.02)
        return got

    def expect(self, patterns, timeout):
        """Wait for one of `patterns` (regex); returns (index, text_before_and_incl_match)."""
        end = time.time() + timeout
        comp = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        while time.time() < end:
            self._pump()
            for i, p in enumerate(comp):
                m = p.search(self.buf)
                if m:
                    out, self.buf = self.buf[:m.end()], self.buf[m.end():]
                    return i, out
            if self.chan.closed:
                out, self.buf = self.buf, ""
                return -1, out
            if "--More--" in self.buf:
                self.buf = self.buf.replace("--More--", "")
                self.chan.send(" ")
        raise BootstrapError(f"timeout; laatste output: {self.mask(self.buf[-200:])!r}")

    def send(self, line):
        try:
            self.chan.send(line + "\n")
        except (OSError, EOFError):   # session already dropped (e.g. IP moved)
            pass

    def run(self, line, timeout=30, shown=None):
        self.send(line)
        i, out = self.expect([PROMPT], timeout)
        if i == -1:
            return out
        if ERR.search(out.split("\n", 1)[-1]):
            raise BootstrapError(f"opdracht mislukt: {shown or self.mask(line)} -> {self.mask(out.strip()[-160:])}")
        return out

    def close(self):
        for o in (self.chan, self.client):
            try:
                if o:
                    o.close()
            except Exception:
                pass


def ssh_connect(host, user, password, port=22, mask=None, log=lambda *_: None, timeout=30):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=port, username=user, password=password, look_for_keys=False, allow_agent=False,
              timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    chan = c.invoke_shell(width=250, height=1000)
    return Shell(chan, mask or Masker(), log, client=c)


def login(connector, host, user, initial_pw, new_pw, mask, log, timing):
    """Open a shell; handles FortiOS' forced password change. Returns (shell, password_in_effect)."""
    pw = initial_pw
    try:
        sh = connector(host, user, initial_pw)
    except paramiko.AuthenticationException:
        # device already has the admin password set (not factory): same password for admin and SSH
        if not new_pw or new_pw == initial_pw:
            raise
        sh = connector(host, user, new_pw)
        pw = new_pw
    for _ in range(6):
        i, out = sh.expect([PROMPT, r"(?i)new password:", r"(?i)confirm password:",
                            r"(?i)please input new password|change your password"], timing.login)
        if i == 0:
            return sh, pw
        if i in (1, 2, 3):
            if not new_pw:
                raise BootstrapError(f"{host}: apparaat vraagt om een nieuw wachtwoord, maar er is geen nieuw wachtwoord opgegeven")
            if i in (1, 2):
                sh.send(new_pw)
                pw = new_pw
            continue
        raise BootstrapError(f"{host}: verbinding gesloten tijdens inloggen")
    raise BootstrapError(f"{host}: wachtwoordwissel liep niet door")


def wait_tcp(host, port, up, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=3):
                if up:
                    return True
        except OSError:
            if not up:
                return True
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- flows
def apply_license(sh, token, mask, log, timing):
    """Send `execute vm-license <token>`; the device reboots on success. Raises on rejection."""
    sh.send(license_line(token))
    log("info", "execute vm-license ******** verstuurd")
    seen = ""
    for _ in range(3):
        i, out = sh.expect([r"\(y/n\)", PROMPT], timing.cmd * 2)
        seen += out
        if i == 0:
            sh.send("y")
            continue
        break
    low = seen.lower()
    if re.search(r"fail|invalid|error|expired|not (found|valid)|unable", low) and "succe" not in low:
        raise BootstrapError("licentie geweigerd: " + mask(seen.strip()[-200:]))
    return seen


def parse_status(text):
    d = {}
    for key, pat in (("serial", r"Serial[- ]Number:\s*(\S+)"), ("license", r"License Status:\s*(\S+)"),
                     ("version", r"Version:\s*(.+)")):
        m = re.search(pat, text)
        if m:
            d[key] = m.group(1).strip()
    return d


def bootstrap_device(kind, name, lines, initial_ip, target_ip, token, creds, log,
                     connector=None, waiter=wait_tcp, timing=None, expect_serial=None, ssh_port=22):
    """kind: 'fgt' | 'fmg'. creds: dict(user, initial_pw, new_pw)."""
    timing = timing or Timing()
    mask = Masker(token, creds.get("initial_pw"), creds.get("new_pw"))
    connector = connector or (lambda h, u, p: ssh_connect(h, u, p, port=ssh_port, mask=mask, log=log))
    user = creds.get("user") or "admin"

    log("info", f"{name}: verbinden met {initial_ip}")
    sh, pw = login(connector, initial_ip, user, creds.get("initial_pw", ""), creds.get("new_pw", ""), mask, log, timing)
    try:
        if kind == "fgt":
            sh.run("config system console", timing.cmd)
            sh.run("set output standard", timing.cmd)
            sh.run("end", timing.cmd)
        log("info", f"{name}: netwerkconfig ({len(lines)} regels)")
        for l in lines[:-1]:
            sh.run(l, timing.cmd)
        # last line closes the interface block and moves the IP; the session may drop here
        sh.send(lines[-1])
        sh.expect([PROMPT], 10) if target_ip == initial_ip else time.sleep(1)
    except BootstrapError:
        sh.close()
        raise
    sh.close()

    reach = initial_ip
    if target_ip != initial_ip:
        log("info", f"{name}: wachten op {target_ip}")
        if not waiter(target_ip, ssh_port, True, timing.link_up):
            raise BootstrapError(f"{name}: {target_ip} niet bereikbaar vanaf de deploy-container "
                                 f"(routing/VLAN naar het WAN-netwerk?)")
        reach = target_ip
    sh, pw = login(connector, reach, user, pw, creds.get("new_pw", ""), mask, log, timing)
    try:
        log("info", f"{name}: FortiFlex-licentie toepassen")
        seen = apply_license(sh, token, mask, log, timing)
    finally:
        sh.close()
    log("info", f"{name}: licentie geaccepteerd, apparaat herstart ({mask(seen.strip().splitlines()[-1] if seen.strip() else '')})")

    waiter(reach, ssh_port, False, timing.reboot_down)
    if not waiter(reach, ssh_port, True, timing.reboot_up):
        raise BootstrapError(f"{name}: apparaat komt niet terug op {reach} na de licentie-reboot")
    sh, pw = login(connector, reach, user, pw, creds.get("new_pw", ""), mask, log, timing)
    try:
        status = parse_status(sh.run("get system status", timing.cmd))
    finally:
        sh.close()
    if kind == "fgt" and expect_serial and status.get("serial") and status["serial"].upper() != expect_serial.upper():
        raise BootstrapError(f"{name}: serienummer {status['serial']} wijkt af van opgegeven {expect_serial}")
    log("ok", f"{name}: klaar - {status.get('serial', '?')} licentie={status.get('license', '?')}")
    return status
