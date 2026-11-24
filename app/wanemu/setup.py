"""Deploy the wan-emu container: create (or reuse) the LXC through the Proxmox API, then provision it over SSH.

Proxmox has no API to run commands inside a container, so the tool puts its own SSH public key in the container at
creation time and logs in as root on the uplink address. Everything here is idempotent: a second deployment reuses
the container (same hostname + tag), applies the current network config and provisions it again."""
import io
import ipaddress
import json
import re
import shlex
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..model import TAG_VM
from . import net

PACKAGES = "dnsmasq nftables iproute2 python3"
AGENT_SRC = Path(__file__).with_name("agent.py")
# not changeable on an existing container (or only meaningful at creation)
CREATE_ONLY = {"vmid", "ostemplate", "storage", "rootfs", "unprivileged", "start", "ssh-public-keys"}


class WanEmuError(Exception):
    pass


def new_ssh_key():
    k = Ed25519PrivateKey.generate()
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                           serialization.NoEncryption()).decode()


def public_key(pem):
    k = serialization.load_ssh_private_key(pem.encode(), password=None)
    return k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


class ParamikoShell:
    def __init__(self, host, pem, timeout=20):
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())    # the container is new on every deployment
        self.c.connect(host, username="root", pkey=paramiko.Ed25519Key.from_private_key(io.StringIO(pem)),
                       timeout=timeout, allow_agent=False, look_for_keys=False)

    def run(self, cmd, input=None):
        stdin, out, err = self.c.exec_command(cmd, timeout=900)
        if input is not None:
            stdin.write(input)
            stdin.channel.shutdown_write()
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode(errors="replace"), err.read().decode(errors="replace")

    def put(self, path, content, mode=0o644):
        with self.c.open_sftp() as s:
            with s.file(path, "w") as f:
                f.write(content)
            s.chmod(path, mode)

    def close(self):
        self.c.close()


def _uplink_ip(t):
    return str(ipaddress.ip_interface(t["wanemu"]["uplink"]["ip"]).ip)


def _unit(t):
    w = t["wanemu"]
    return ("[Unit]\nDescription=fgt-sdwan-provision WAN-emulator agent\nAfter=network-online.target\n"
            "Wants=network-online.target\n\n[Service]\n"
            # systemd-sysctl skips itself in a container (/proc/sys read-only), so forwarding is set here on every start
            "ExecStartPre=-/usr/sbin/sysctl -q -p /etc/sysctl.d/90-wan-emu.conf\n"
            f"ExecStart=/usr/bin/python3 /opt/wan-emu/agent.py --conf /etc/wan-emu --listen {_uplink_ip(t)} "
            f"--port {w['agent_port']}\nRestart=always\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n")


SYSCTL = ("net.ipv4.ip_forward=1\nnet.ipv4.conf.all.rp_filter=0\nnet.ipv4.conf.default.rp_filter=0\n")


def _must(sh, cmd, what):
    rc, out, err = sh.run(cmd)
    if rc != 0:
        raise WanEmuError(f"{what}: {(err or out).strip()[:300]}")
    return out


def provision(sh, t, agent_token, emit, step="pve_wan"):
    """Packages, netem/ifb check, forwarding, DHCP, agent. Safe to run again."""
    if sh.run(f"dpkg -s {PACKAGES} >/dev/null 2>&1")[0] != 0:
        emit(step, "running", f"installing packages: {PACKAGES}")
        _must(sh, f"DEBIAN_FRONTEND=noninteractive apt-get update -q && "
                  f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q {PACKAGES}", "installing packages failed")
    seg_if = (net.links(t) or [{"iface": "eth1"}])[0]["iface"]
    if sh.run("ip link add ifb-test type ifb && ip link del ifb-test")[0] != 0 or \
            sh.run(f"tc qdisc replace dev {seg_if} root netem delay 0ms && tc qdisc del dev {seg_if} root")[0] != 0:
        raise WanEmuError("netem/ifb do not work in the container: load sch_netem and ifb on the PVE host "
                          "(modprobe sch_netem ifb; permanently via /etc/modules-load.d/wan-emu.conf)")
    sh.put("/etc/sysctl.d/90-wan-emu.conf", SYSCTL)
    _must(sh, "sysctl -q -p /etc/sysctl.d/90-wan-emu.conf", "turning on forwarding failed")   # not --system: kernel.* is read-only in a CT
    sh.put("/etc/dnsmasq.d/wan-emu.conf", net.dnsmasq_conf(t))
    _must(sh, "systemctl enable -q dnsmasq && systemctl restart dnsmasq", "starting dnsmasq failed")
    _must(sh, "mkdir -p /etc/wan-emu /opt/wan-emu && chmod 700 /etc/wan-emu", "creating directories failed")
    sh.put("/etc/wan-emu/topology.json", json.dumps(net.agent_topology(t), indent=1))
    sh.put("/etc/wan-emu/token", agent_token, 0o600)
    sh.put("/opt/wan-emu/agent.py", AGENT_SRC.read_text(), 0o755)
    sh.put("/etc/systemd/system/wan-emu-agent.service", _unit(t))
    _must(sh, "systemctl daemon-reload && systemctl enable -q wan-emu-agent && systemctl restart wan-emu-agent",
          "starting the WAN emulator agent failed")
    # the agent applies tc + nft (incl. NAT) before it listens: listening = shaping and NAT are loaded
    port = t["wanemu"]["agent_port"]
    if sh.run(f"for i in $(seq 1 30); do systemctl is-active -q wan-emu-agent && ss -ltn | grep -q ':{port} ' && exit 0; "
              "sleep 1; done; exit 1")[0] != 0:
        log = sh.run("journalctl -u wan-emu-agent -n 15 --no-pager")[1].strip()
        raise WanEmuError("WAN emulator agent does not come up (journalctl -u wan-emu-agent): " + (log[-600:] or "no log"))


def internet_check(sh, t):
    first = next((x for x in net.links(t) if x["seg"] != "hub"), None) or (net.links(t) or [{"gw": ""}])[0]
    src = first["gw"]
    return sh.run(f"ping -c2 -W2 -I {shlex.quote(src)} 1.1.1.1")[0] == 0


def _ver_key(name):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


def ensure_template(pve, t, emit, step="pve_wan", template=None):
    """Make sure a CT template exists, like `pveam download`: the wan-emu one (fixed: debian-13-standard) or the given
    `template`. Uses that file when present, else the newest of the same family/arch already on that storage, else
    downloads the newest one from the Proxmox appliance index. Returns the volume used; for the wan-emu template it is
    also set in t['wanemu']['template']."""
    own = template is None
    out = _ensure_template(pve, t["pve"]["node"], template or t["wanemu"]["template"], emit, step)
    if own:
        t["wanemu"]["template"] = out
    return out


def _ensure_template(pve, node, vol, emit, step):
    from ..pve import PveError
    storage, fname = vol.split(":", 1)[0], vol.rsplit("/", 1)[-1]
    family, arch = fname.split("_", 1)[0], fname.rsplit("_", 1)[-1]  # debian-13-standard, amd64.tar.zst
    same = lambda n: n.startswith(family + "_") and n.endswith("_" + arch)   # not turnkey-*, not arm64
    try:
        have = [c.get("volid", "") for c in pve.list_content(node, storage, "vztmpl")]
    except PveError as e:
        raise WanEmuError(f"CT template: cannot list storage '{storage}' ({e}); it must have content type "
                          "'Container template' (Datacenter > Storage)") from None
    if vol in have:
        return vol
    local = sorted((v for v in have if same(v.rsplit("/", 1)[-1])), key=_ver_key)
    if local:
        emit(step, "running", f"CT template {fname} not on '{storage}', using {local[-1].rsplit('/', 1)[-1]}")
        return local[-1]
    try:
        avail = sorted((a["template"] for a in pve.appliances(node) if same(str(a.get("template", "")))), key=_ver_key)
    except PveError:
        avail = []
    want = avail[-1] if avail else fname
    emit(step, "running", f"CT template {want}: not on '{storage}', downloading it from Proxmox (pveam download)")
    try:
        pve.wait_task(node, pve.download_appliance(node, storage, want))
    except PveError as e:
        raise WanEmuError(f"CT template {want} is missing on '{storage}' and downloading it failed ({e}). Download it on "
                          f"the PVE host: pveam update && pveam download {storage} {want} - or give the API token "
                          "Datastore.AllocateTemplate on that storage") from None
    return f"{storage}:vztmpl/{want}"


def find_ct(pve, name, old):
    """Containers of this tool named `name`, else the one under its name without (this) prefix (renamed on update)."""
    cts = pve.find_cts(TAG_VM)
    return [c for c in cts if c.get("name") == name] or [c for c in cts if c.get("name") == old]


def deploy_wanemu(pve, t, secrets, emit, step="pve_wan", connector=None, waiter=None, vmids=None):
    """Create or reuse + provision the container. Returns {'vmid', 'created', 'internet'}."""
    from ..bootstrap import wait_tcp
    node, w = t["pve"]["node"], t["wanemu"]
    ip = _uplink_ip(t)
    connector = connector or (lambda host, pem: ParamikoShell(host, pem))
    waiter = waiter or wait_tcp
    from ..model import pve_name
    name = pve_name(t, w["hostname"])
    found = find_ct(pve, name, w["hostname"])
    created = not found
    if found:
        vmid = int(found[0]["vmid"])
        emit(step, "running", f"{name}: exists (CT {vmid}), updating its configuration"
                              + (f" (renamed from {found[0]['name']})" if found[0].get("name") != name else ""))
        if w.get("vmid") and int(w["vmid"]) != vmid:
            emit(step, "running", f"{name}: CT id {w['vmid']} ignored, the existing CT {vmid} is used "
                                  "(a CT id cannot change; delete CT {vmid} for a new one)".replace("{vmid}", str(vmid)))
        params = {k: v for k, v in net.ct_params(t, vmid, "").items() if k not in CREATE_ONLY}
        from ..pve import PveError
        try:                                       # a NIC per WAN link: remove the ones of an earlier, larger layout
            cur = pve.ct_config(node, vmid) or {}
        except PveError as e:
            raise WanEmuError(f"{name}: cannot read its configuration (CT {vmid}) to replace its WAN NICs: {e}") from None
        extra = sorted((k for k in cur if re.fullmatch(r"net\d+", k) and k not in params), key=lambda k: int(k[3:]))
        if extra:
            params["delete"] = ",".join(extra)
        if pve.ct_status(node, vmid) == "running":
            pve.wait_task(node, pve.stop_ct(node, vmid))
        pve.set_ct_config(node, vmid, params)
    else:
        from ..provision_vms import pick_vmid
        vmid = int(w.get("vmid") or pick_vmid(pve, vmids, name))
        ensure_template(pve, t, emit, step)
        emit(step, "running", f"{name}: creating CT {vmid} ({w['template'].split('/')[-1]})")
        pve.wait_task(node, pve.create_ct(node, net.ct_params(t, vmid, public_key(secrets["ssh_key"]))))
    try:
        pve.wait_task(node, pve.start_ct(node, vmid))
        emit(step, "running", f"{name}: started, waiting for SSH on {ip}")
        if not waiter(ip, 22, True, 180):
            raise WanEmuError(f"{name} not reachable via SSH on {ip}:22 (check uplink VLAN "
                              f"{w['uplink'].get('vlan') or 'untagged'} on {w['uplink'].get('bridge') or w['bridge']} and gateway "
                              f"{w['uplink']['gw']}; is the Uplink VLAN filled in?)")
        sh = connector(ip, secrets["ssh_key"])
        try:
            provision(sh, t, secrets["agent_token"], emit, step)
            internet = internet_check(sh, t)
        finally:
            sh.close()
    except Exception as e:
        if created:
            emit(step, "running", f"{name}: provisioning failed, deleting CT {vmid}")
            try:
                pve.wait_task(node, pve.stop_ct(node, vmid))
            except Exception:
                pass
            try:
                pve.wait_task(node, pve.delete_ct(node, vmid))
            except Exception:
                pass
        if isinstance(e, WanEmuError):
            raise
        raise WanEmuError(f"{name}: {e}") from e
    if not internet:
        emit(step, "warn", "no internet from ISP-A: check NAT on wan-emu and the uplink "
                           f"{w['uplink']['gw']} (the FortiGates will not get a FortiFlex licence)")
    return {"vmid": vmid, "created": created, "internet": internet}


def deploy_clients(pve, t, password, emit, step="clients", vmids=None):
    """One LAN client CT per branch (client-<name>), reused by name + tag (network config updated). Returns
    [{name, vmid, created, ip}]. No packages are installed: Proxmox cannot run commands in a CT."""
    from ..model import pve_name
    node = t["pve"]["node"]
    have = {c.get("name"): int(c["vmid"]) for c in pve.find_cts(TAG_VM)}
    for e in t["edges"]:                            # deployed before without (this) prefix: reused, renamed on update
        old, new = f"client-{e['name']}", pve_name(t, f"client-{e['name']}")
        if new not in have and old in have:
            have[new] = have.pop(old)
    if any(pve_name(t, f"client-{e['name']}") not in have and (e.get("lans") or [{}])[0].get("ip") for e in t["edges"]):
        ensure_template(pve, t, emit, step)                         # a new client needs the CT template
    out = []
    for e in t["edges"]:
        if not (e.get("lans") or [{}])[0].get("ip"):
            continue
        name = pve_name(t, f"client-{e['name']}")
        if name in have:
            vmid = have[name]
            p = {k: v for k, v in net.client_params(t, e, vmid, password).items() if k not in CREATE_ONLY | {"password"}}
            if pve.ct_status(node, vmid) == "running":
                pve.wait_task(node, pve.stop_ct(node, vmid))
            pve.set_ct_config(node, vmid, p)
            created = False
        else:
            from ..provision_vms import pick_vmid
            vmid = pick_vmid(pve, vmids, name)
            pve.wait_task(node, pve.create_ct(node, net.client_params(t, e, vmid, password)))
            created = True
        pve.wait_task(node, pve.start_ct(node, vmid))
        ip = net.client_params(t, e, vmid, "").get("net0").split("ip=")[1].split(",")[0]
        emit(step, "running", f"{name}: CT {vmid} {'created' if created else 'updated'}, {ip} behind {e['name']}")
        out.append({"name": name, "vmid": vmid, "created": created, "ip": ip})
    return out
