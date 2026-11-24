"""Proxmox consoles for the Access page (termproxy + vncwebsocket).

LXC (the LAN clients): with the tool's API token. VMs (FortiGates, FortiManager): Proxmox 9.1 refuses the serial
console (qemu termproxy) to an API token ("'root@pam!x' does not look like a valid user name"), so they use the
console user of the Proxmox server (POST /access/ticket, cookie + CSRF). A VM needs a serial port (serial0)."""
from urllib.parse import urlencode

import requests

from .pve import Pve, PveError

TAG = "fgt-sdwan"


class ConsoleError(Exception):
    pass


def _pve(row):
    return Pve(row["url"], row["token_id"], row["token_secret"], bool(row["verify_tls"]), timeout=15)


def locate(pve, pve_name):
    """-> {node, kind: "lxc"|"qemu", vmid, status} of the tool's VM/CT with that Proxmox name."""
    try:
        rows = pve._req("GET", "/cluster/resources", params={"type": "vm"}) or []
    except PveError as e:
        raise ConsoleError(f"Proxmox not reachable: {e}") from None
    hit = [r for r in rows if r.get("name") == pve_name and TAG in str(r.get("tags", "")).split(";")]   # only the tool's own
    if not hit:
        raise ConsoleError(f"{pve_name} not found in Proxmox (deployed? deleted?)")
    r = hit[0]
    return {"node": r.get("node"), "kind": r.get("type"), "vmid": int(r["vmid"]), "status": r.get("status")}


def _no_console(e, pve_name):
    if getattr(e, "status", None) == 403 or "403" in str(e):
        return ConsoleError(f"{pve_name}: the Proxmox {'user' if 'user' in str(e) else 'token/user'} lacks VM.Console "
                            "(Datacenter > Permissions: role PVEVMUser on /vms)")
    return ConsoleError(f"{pve_name}: console not available: {e}")


def open_session(row, console, pve_name):
    """-> {ws_url, headers, auth_line, ssl_verify} for the websocket to Proxmox. console = {"user", "password"} of
    the server's console user (needed for VMs only)."""
    pve = _pve(row)
    loc = locate(pve, pve_name)
    if loc["status"] != "running":
        raise ConsoleError(f"{pve_name} is not running (status {loc['status']}): start it first")
    node, kind, vmid = loc["node"], loc["kind"], loc["vmid"]
    path = f"/nodes/{node}/{kind}/{vmid}"
    if kind == "lxc":
        try:
            d = pve._req("POST", f"{path}/termproxy")
        except PveError as e:
            raise _no_console(e, pve_name) from None
        headers = dict(pve.headers)
    else:
        if not console:
            raise ConsoleError(f"{pve_name} is a VM: add a console user to the Proxmox server (Proxmox servers > "
                               "edit) for VM consoles; SSH works without one")
        try:
            cfg = pve._req("GET", f"{path}/config") or {}
        except PveError as e:
            raise ConsoleError(f"{pve_name}: VM config not readable: {e}") from None
        if not cfg.get("serial0"):
            raise ConsoleError(f"{pve_name} has no serial port: deploy it again, or add serial0 (Hardware > Add > "
                               "Serial Port) + Display 'Serial terminal 0' and restart the VM")
        try:
            r = requests.post(pve.base + "/access/ticket", data={"username": console["user"],
                              "password": console["password"]}, verify=pve.verify, timeout=15)
        except requests.RequestException as e:
            raise ConsoleError(f"Proxmox not reachable: {type(e).__name__}") from None
        if r.status_code != 200:
            u = console["user"]
            hint = " root is root@pam, not root@pve." if u.split("@")[0] == "root" and not u.endswith("@pam") else ""
            raise ConsoleError(f"console login refused for {u}: wrong user, realm or password (realm after the @: "
                               f"pam = Linux user of the Proxmox host, pve = Proxmox user; 2FA is not supported).{hint} "
                               "Change it under Servers > edit")
        t = r.json()["data"]
        headers = {"Cookie": f"PVEAuthCookie={t['ticket']}"}
        try:
            tp = requests.post(pve.base + f"{path}/termproxy", headers=dict(headers, CSRFPreventionToken=t["CSRFPreventionToken"]),
                               verify=pve.verify, timeout=15)
        except requests.RequestException as e:
            raise ConsoleError(f"Proxmox not reachable: {type(e).__name__}") from None
        if tp.status_code != 200:
            raise _no_console(PveError(f"Proxmox {tp.status_code}: user {tp.reason}", status=tp.status_code), pve_name)
        d = tp.json()["data"]
    ws = pve.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return {"ws_url": f"{ws}{path}/vncwebsocket?" + urlencode({"port": d["port"], "vncticket": d["ticket"]}),
            "headers": headers, "auth_line": f"{d['user']}:{d['ticket']}\n", "ssl_verify": pve.verify}
