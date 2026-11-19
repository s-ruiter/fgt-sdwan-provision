"""Deploy the wan-emu container: create (or reuse) the LXC through the Proxmox API, then provision it over SSH.

Proxmox has no API to run commands inside a container, so the tool puts its own SSH public key in the container at
creation time and logs in as root on the uplink address. Everything here is idempotent: a second deployment reuses
the container (same hostname + tag), applies the current network config and provisions it again."""
import io
import ipaddress
import json
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
    seg_if = net.SEG_IFACE["hub"]
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
    src = t["wanemu"]["segments"]["a"]["gw"]
    return sh.run(f"ping -c2 -W2 -I {shlex.quote(src)} 1.1.1.1")[0] == 0


def deploy_wanemu(pve, t, secrets, emit, step="pve_wan", connector=None, waiter=None):
    """Create or reuse + provision the container. Returns {'vmid', 'created', 'internet'}."""
    from ..bootstrap import wait_tcp
    node, w = t["pve"]["node"], t["wanemu"]
    ip = _uplink_ip(t)
    connector = connector or (lambda host, pem: ParamikoShell(host, pem))
    waiter = waiter or wait_tcp
    found = [c for c in pve.find_cts(TAG_VM) if c.get("name") == w["hostname"]]
    created = not found
    if found:
        vmid = int(found[0]["vmid"])
        emit(step, "running", f"{w['hostname']}: exists (CT {vmid}), updating its configuration")
        if w.get("vmid") and int(w["vmid"]) != vmid:
            emit(step, "running", f"{w['hostname']}: CT id {w['vmid']} ignored, the existing CT {vmid} is used "
                                  "(a CT id cannot change; delete CT {vmid} for a new one)".replace("{vmid}", str(vmid)))
        params = {k: v for k, v in net.ct_params(t, vmid, "").items() if k not in CREATE_ONLY}
        if pve.ct_status(node, vmid) == "running":
            pve.wait_task(node, pve.stop_ct(node, vmid))
        pve.set_ct_config(node, vmid, params)
    else:
        vmid = int(w.get("vmid") or pve.next_vmid())
        emit(step, "running", f"{w['hostname']}: creating CT {vmid} ({w['template'].split('/')[-1]})")
        pve.wait_task(node, pve.create_ct(node, net.ct_params(t, vmid, public_key(secrets["ssh_key"]))))
    try:
        pve.wait_task(node, pve.start_ct(node, vmid))
        emit(step, "running", f"{w['hostname']}: started, waiting for SSH on {ip}")
        if not waiter(ip, 22, True, 180):
            raise WanEmuError(f"{w['hostname']} not reachable via SSH on {ip}:22 (check uplink VLAN "
                              f"{w['uplink']['vlan']} and gateway {w['uplink']['gw']})")
        sh = connector(ip, secrets["ssh_key"])
        try:
            provision(sh, t, secrets["agent_token"], emit, step)
            internet = internet_check(sh, t)
        finally:
            sh.close()
    except Exception as e:
        if created:
            emit(step, "running", f"{w['hostname']}: provisioning failed, deleting CT {vmid}")
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
        raise WanEmuError(f"{w['hostname']}: {e}") from e
    if not internet:
        emit(step, "warn", "no internet from ISP-A: check NAT on wan-emu and the uplink "
                           f"{w['uplink']['gw']} (the FortiGates will not get a FortiFlex licence)")
    return {"vmid": vmid, "created": created, "internet": internet}


def deploy_clients(pve, t, password, emit, step="clients"):
    """One LAN client CT per branch (client-<name>), reused by name + tag (network config updated). Returns
    [{name, vmid, created, ip}]. No packages are installed: Proxmox cannot run commands in a CT."""
    node = t["pve"]["node"]
    have = {c.get("name"): int(c["vmid"]) for c in pve.find_cts(TAG_VM)}
    out = []
    for e in t["edges"]:
        if not (e.get("lans") or [{}])[0].get("ip"):
            continue
        name = f"client-{e['name']}"
        if name in have:
            vmid = have[name]
            p = {k: v for k, v in net.client_params(t, e, vmid, password).items() if k not in CREATE_ONLY | {"password"}}
            if pve.ct_status(node, vmid) == "running":
                pve.wait_task(node, pve.stop_ct(node, vmid))
            pve.set_ct_config(node, vmid, p)
            created = False
        else:
            vmid = pve.next_vmid()
            pve.wait_task(node, pve.create_ct(node, net.client_params(t, e, vmid, password)))
            created = True
        pve.wait_task(node, pve.start_ct(node, vmid))
        ip = net.client_params(t, e, vmid, "").get("net0").split("ip=")[1].split(",")[0]
        emit(step, "running", f"{name}: CT {vmid} {'created' if created else 'updated'}, {ip} behind {e['name']}")
        out.append({"name": name, "vmid": vmid, "created": created, "ip": ip})
    return out
