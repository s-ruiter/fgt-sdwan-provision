"""Traffic-Gen (https://github.com/s-ruiter/Traffic-Gen), optional: a container that generates realistic user traffic
as clients in the LAN of the first spoke, managed over the WAN emulator uplink (the lab network).

eth0 = management on the uplink bridge/VLAN with its own static IP (web GUI https://<ip>:8443, also how this tool
logs in over SSH to install it); eth1 = test traffic on the spoke's LAN1 VLAN without an IP: Traffic-Gen puts the source
IPs (.20 and up) on it with source-based routing via the spoke. Like wan-emu the container is created through the
Proxmox API with this tool's SSH key and installed over SSH (Proxmox cannot run commands in a container)."""
import ipaddress
import json
import shlex

from .model import TAG_VM
from .wanemu import net
from .wanemu.setup import CREATE_ONLY, ParamikoShell, ensure_template

HOSTNAME = "trafficgen"
TEMPLATE = "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"   # what Traffic-Gen's own installer uses
REPO_ARCHIVE = "https://github.com/s-ruiter/Traffic-Gen/archive/main.tar.gz"
PORT = 8443
FIRST = 20                                     # source IPs .20 .. .20+count-1 (the LAN client has .10)
PACKAGES = "python3 python3-yaml util-linux iproute2 iputils-ping iputils-arping openssl logrotate ca-certificates curl"


class TrafficGenError(Exception):
    pass


def enabled(t):
    return bool((t.get("trafficgen") or {}).get("enabled"))


def spoke(t):
    """The first spoke with a LAN: Traffic-Gen sends its traffic from that LAN."""
    return next((e for e in t.get("edges", []) if (e.get("lans") or [{}])[0].get("ip")), None)


def _sources(t):
    """(first source IP, last source IP, LAN interface of the spoke)."""
    lan = ipaddress.ip_interface(spoke(t)["lans"][0]["ip"])
    first = lan.network.network_address + FIRST
    return first, first + (int(t["trafficgen"]["count"]) - 1), lan


def validate(t):
    if not enabled(t):
        return []
    g, up, err = t["trafficgen"], t["wanemu"]["uplink"], []
    try:
        upif = ipaddress.ip_interface(str(up.get("ip", "")).strip())
    except ValueError:
        return ["Traffic-Gen: fill in the WAN emulation uplink first (its management runs over the uplink network)"]
    try:
        ip = ipaddress.ip_interface(str(g.get("ip", "")).strip())
    except ValueError:
        ip = None
    if ip is None or "/" not in str(g.get("ip", "")):
        err.append("Traffic-Gen: management IP as a.b.c.d/nn is required (a free address in the uplink network)")
    elif ip.network != upif.network:
        err.append(f"Traffic-Gen: management IP must be in the uplink network {upif.network}")
    elif ip.ip == upif.ip:
        err.append(f"Traffic-Gen: management IP {ip.ip} is the address of wan-emu")
    elif str(ip.ip) == str(up.get("gw", "")).strip():
        err.append(f"Traffic-Gen: management IP {ip.ip} is the uplink gateway")
    if spoke(t) is None:
        err.append("Traffic-Gen: needs a spoke with a LAN (it sends from the LAN of the first spoke)")
        return err
    try:
        first, last, lan = _sources(t)
    except ValueError:
        return err                               # the LAN itself is reported by the topology validation
    if not (first in lan.network and last in lan.network):
        err.append(f"Traffic-Gen: source IPs {first}-{last} do not fit in {lan.network}")
    elif first <= lan.ip <= last:
        err.append(f"Traffic-Gen: the LAN address {lan.ip} of {spoke(t)['name']} is inside the source IPs {first}-{last}")
    return err


def ct_params(t, vmid, ssh_pubkey, template):
    w, g, up = t["wanemu"], t["trafficgen"], t["wanemu"]["uplink"]
    lan1 = net.lan_vlans(t)[spoke(t)["name"]][0]
    net0 = f"name=eth0,bridge={up.get('bridge') or w['bridge']}" + (f",tag={up['vlan']}" if up.get("vlan") else "")
    from .model import pve_name
    return {"vmid": vmid, "hostname": pve_name(t, HOSTNAME), "ostemplate": template, "storage": w["storage"],
            "rootfs": f"{w['storage']}:4", "cores": 2, "memory": 1024, "swap": 256, "unprivileged": 1,
            "features": "nesting=1", "onboot": 1, "start": 0, "tags": TAG_VM, "ssh-public-keys": ssh_pubkey,
            "description": "Traffic-Gen: traffic generator in the LAN of the first spoke (fgt-sdwan-provision)",
            "nameserver": " ".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x),
            "net0": net0 + f",ip={g['ip']},gw={up['gw']},firewall=0",
            "net1": f"name=eth1,bridge={w['bridge']},tag={lan1},firewall=0"}


def config(t):
    """What this tool sets in /etc/trafficgen/config.yaml (merged into an existing one, other settings are kept)."""
    first, _, lan = _sources(t)
    return {"mgmt_interface": "eth0",
            "networks": [{"name": f"{spoke(t)['name']}-lan1", "parent": "eth1", "vlan": None, "first": str(first),
                          "prefix": lan.network.prefixlen, "gateway": str(lan.ip), "count": int(t["trafficgen"]["count"]),
                          "dns": t.get("dns_primary") or ""}]}


UNIT = ("[Unit]\nDescription=Traffic-Gen web GUI and engine\nAfter=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nEnvironment=TRAFFICGEN_PORT={PORT}\nExecStart=/usr/bin/python3 /opt/trafficgen/webui.py\n"
        "Restart=on-failure\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n")
LOGROTATE = "/var/log/trafficgen/*.log {\n  weekly\n  rotate 4\n  compress\n  missingok\n  notifempty\n  copytruncate\n}\n"
MERGE = ("import json, yaml, os\np = '/etc/trafficgen/config.yaml'\n"
         "c = (yaml.safe_load(open(p)) or {}) if os.path.exists(p) else {}\n"
         "c.update(json.load(open('/root/.tg-networks.json')))\nyaml.safe_dump(c, open(p, 'w'), sort_keys=False)\n")


def _must(sh, cmd, what):
    rc, out, err = sh.run(cmd)
    if rc != 0:
        raise TrafficGenError(f"Traffic-Gen: {what}: {(err or out).strip()[:300]}")
    return out


def provision(sh, t, password, emit, step):
    """Same steps as Traffic-Gen's install.sh, done over SSH; safe to run again (updates the code, keeps the GUI's
    other settings and logs)."""
    ip = str(ipaddress.ip_interface(t["trafficgen"]["ip"]).ip)
    if sh.run(f"dpkg -s {PACKAGES} >/dev/null 2>&1")[0] != 0:
        emit(step, "running", "Traffic-Gen: installing packages")
        _must(sh, f"DEBIAN_FRONTEND=noninteractive apt-get update -q && DEBIAN_FRONTEND=noninteractive "
                  f"apt-get install -y -q --no-install-recommends {PACKAGES}", "installing packages failed")
    emit(step, "running", "Traffic-Gen: downloading the latest version from GitHub")
    _must(sh, f"rm -rf /tmp/tg && mkdir -p /tmp/tg && curl -fsSL {REPO_ARCHIVE} | tar -xz -C /tmp/tg --strip-components=1 "
              "&& test -f /tmp/tg/src/trafficgen.py", "downloading Traffic-Gen from GitHub failed")
    _must(sh, "mkdir -p /opt/trafficgen/web /etc/trafficgen/tls /var/log/trafficgen /var/lib/trafficgen && "
              "cp /tmp/tg/src/*.py /tmp/tg/src/*.yaml /opt/trafficgen/ && cp /tmp/tg/src/web/* /opt/trafficgen/web/ && "
              "chmod 755 /opt/trafficgen/trafficgen.py && ln -sf /opt/trafficgen/trafficgen.py /usr/local/bin/trafficgen "
              "&& rm -rf /tmp/tg", "installing the files failed")
    _must(sh, f"openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN={HOSTNAME} "
              f"-addext subjectAltName=DNS:{HOSTNAME},IP:{shlex.quote(ip)} -keyout /etc/trafficgen/tls/server.key "
              "-out /etc/trafficgen/tls/server.crt 2>/dev/null && chmod 600 /etc/trafficgen/tls/server.key",
          "creating the TLS certificate failed")
    sh.put("/root/.tg-pw", password, 0o600)                     # never on a command line
    try:
        _must(sh, "/usr/bin/python3 /opt/trafficgen/trafficgen.py passwd --file /root/.tg-pw", "setting the GUI password (passwd) failed")
    finally:
        sh.run("rm -f /root/.tg-pw")
    sh.put("/root/.tg-networks.json", json.dumps(config(t)), 0o600)
    _must(sh, f"/usr/bin/python3 -c {shlex.quote(MERGE)} && rm -f /root/.tg-networks.json", "writing the configuration failed")
    sh.put("/etc/systemd/system/trafficgen-web.service", UNIT)
    sh.put("/etc/logrotate.d/trafficgen", LOGROTATE)
    _must(sh, "systemctl daemon-reload && systemctl enable -q trafficgen-web && systemctl restart trafficgen-web",
          "starting the service failed")
    if sh.run(f"for i in $(seq 1 30); do ss -ltn | grep -q ':{PORT} ' && exit 0; sleep 1; done; exit 1")[0] != 0:
        log = sh.run("journalctl -u trafficgen-web -n 15 --no-pager")[1].strip()
        raise TrafficGenError("Traffic-Gen: the web GUI does not come up (journalctl -u trafficgen-web): " + (log[-500:] or "no log"))


def deploy(pve, t, ssh_key, password, emit, step="clients", connector=None, waiter=None, vmids=None):
    """Create or reuse (hostname + tag) and install. Returns {'vmid', 'created', 'url'}."""
    from .bootstrap import wait_tcp
    from .wanemu.setup import public_key
    node, up = t["pve"]["node"], t["wanemu"]["uplink"]
    ip = str(ipaddress.ip_interface(t["trafficgen"]["ip"]).ip)
    connector = connector or (lambda host, pem: ParamikoShell(host, pem))
    waiter = waiter or wait_tcp
    from .model import pve_name
    from .wanemu.setup import find_ct
    found = find_ct(pve, pve_name(t, HOSTNAME), HOSTNAME)
    created = not found
    if found:
        vmid = int(found[0]["vmid"])
        emit(step, "running", f"Traffic-Gen: exists (CT {vmid}), updating it")
        params = {k: v for k, v in ct_params(t, vmid, "", "").items() if k not in CREATE_ONLY}
        if pve.ct_status(node, vmid) == "running":
            pve.wait_task(node, pve.stop_ct(node, vmid))
        pve.set_ct_config(node, vmid, params)
    else:
        from .provision_vms import pick_vmid
        vmid = int(pick_vmid(pve, vmids, pve_name(t, HOSTNAME)))
        tmpl = ensure_template(pve, t, emit, step, template=TEMPLATE)
        emit(step, "running", f"Traffic-Gen: creating CT {vmid} ({tmpl.rsplit('/', 1)[-1]})")
        pve.wait_task(node, pve.create_ct(node, ct_params(t, vmid, public_key(ssh_key), tmpl)))
    try:
        pve.wait_task(node, pve.start_ct(node, vmid))
        emit(step, "running", f"Traffic-Gen: started, waiting for SSH on {ip}")
        if not waiter(ip, 22, True, 180):
            raise TrafficGenError(f"Traffic-Gen not reachable via SSH on {ip}:22 (management on the uplink: VLAN "
                                  f"{up.get('vlan') or 'untagged'}, gateway {up.get('gw')}; is {ip} free?)")
        sh = connector(ip, ssh_key)
        try:
            provision(sh, t, password, emit, step)
        finally:
            sh.close()
    except Exception as e:
        if created:
            emit(step, "running", f"Traffic-Gen: installation failed, deleting CT {vmid}")
            for f in (pve.stop_ct, pve.delete_ct):
                try:
                    pve.wait_task(node, f(node, vmid))
                except Exception:
                    pass
        if isinstance(e, TrafficGenError):
            raise
        raise TrafficGenError(f"Traffic-Gen: {e}") from e
    return {"vmid": vmid, "created": created, "url": f"https://{ip}:{PORT}"}
