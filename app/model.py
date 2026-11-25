"""Topology model: defaults, validation and translation to Jinja Orchestrator inputs."""
import copy
import ipaddress
import json
import re

DEFAULT_INITIAL_IP = "192.168.1.99"   # factory default of FortiGate-VM / FortiManager-VM port1
TAG_VM = "fgt-sdwan"                  # Proxmox tag on every VM / CT the tool creates


# WAN design (user decision): a hub has ONE wan (wan1), always static; a branch has wan1 + optional wan2, both
# always DHCP. Every branch wan builds a tunnel to wan1 of every hub. `wan1_mode` stays as an internal key (other
# code reads it) but is forced by normalize_wans(): hub = static, branch = dhcp.
def _hub(name, lo, wan1, lan):
    return {"name": name, "serial": "", "loopback": lo,
            "wan1_intf": "port1", "wan1_mode": "static", "wan1_ip": f"{wan1}/24", "wan1_gw": "100.64." + wan1.split(".")[2] + ".1",
            "initial_ip": DEFAULT_INITIAL_IP, "lans": [{"intf": "port5", "ip": lan}]}


def _edge(name, lo, n, wan2=True):
    return {"name": name, "serial": "", "loopback": lo,
            "wan1_intf": "port1", "wan2_intf": "port2" if wan2 else "",
            "wan1_mode": "dhcp", "initial_ip": DEFAULT_INITIAL_IP,
            "lans": [{"intf": "port5", "ip": f"10.0.{n}.1/24"}, {"intf": "port6", "ip": f"10.0.10{n}.1/24"}]}


DEFAULTS = {
    "adom": "SDWAN-MV",
    "os_ver": "8.0",
    "mr": 0,
    "platform": "FortiGate-VM64",
    "psk": "",
    "hub2hub": False,
    "lo_summary": "10.200.0.0/14",
    "dns_primary": "1.1.1.1",
    "dns_secondary": "8.8.8.8",
    "wan1_access": "ping https ssh fgfm",   # allowaccess on wan1: keeps admin + FMG reachable after the push
    "fmgr": {                               # FortiManager: always a fixed address
        "name": "fmg",
        "ip": "",                           # a.b.c.d/nn
        "gateway": "",
        "port": "port1",
        "initial_ip": DEFAULT_INITIAL_IP,
    },
    "pve": {                                # Proxmox target for day-0 deployment (secret token is sent separately)
        "server_id": None, "url": "", "token_id": "", "verify_tls": True, "node": "",
        "disk_storage": "", "import_storage": "", "iso_storage": "",
        "default_bridge": "vmbr0", "cpu_type": "host", "license_mode": "cli",
        "first_vmid": None,                 # start of the id block of a deployment (None = lowest free block from 100)
        "name_prefix": "",                  # prefix for the Proxmox VM/CT names of this deployment ("" = none; "-" added)
        "ports": {f"port{i}": {"bridge": "", "vlan": None} for i in range(1, 9)},
        "fmg": {"bridge": "", "vlan": None, "cores": 4, "memory_mb": 16384, "data_disk_gb": 100},
        "fgt": {"cores": 2, "memory_mb": 2048},
    },
    # WAN emulator (LXC wan-emu): always deployed with Day-0 FortiGates. ISP-HUB = hub wan1 (static), ISP-A = branch
    # wan1, ISP-B = branch wan2 (both DHCP from the container). The container gateway is .1 of each segment.
    "wanemu": {
        "vmid": None, "hostname": "wan-emu", "template": "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst",
        "storage": "local-lvm", "bridge": "vmbr0", "cores": 1, "memory_mb": 512, "disk_gb": 4, "agent_port": 8090,
        # port forwarding on the uplink: FortiGate n (hubs, then branches) on <base>+n -> its WAN1 443 / 22
        "fwd_https_base": 10443, "fwd_ssh_base": 10022,
        # LAN VLANs: LAN1 of FortiGate n on lan_vlan_base+1+n, LAN2 on lan2_vlan_base+1+n
        "lan_vlan_base": 3300, "lan2_vlan_base": 3400,
        "uplink": {"bridge": "", "vlan": None, "ip": "", "gw": ""},   # bridge "" = the WAN emulator bridge;   # lab-specific: filled in by the user (VLAN empty = untagged)
        # one VLAN + /24 per FortiGate WAN: link n of an ISP = n-th /24 of its block (.1 wan-emu, .2 FortiGate) on
        # VLAN vlan_base+n. Hub n wan1 -> ISP-HUB, spoke n wan1 -> ISP-A, spoke n wan2 -> ISP-B.
        "segments": {
            "hub": {"name": "ISP-HUB", "block": "100.64.0.0/16", "vlan_base": 3200},
            "a": {"name": "ISP-A", "block": "100.65.0.0/16", "vlan_base": 3210},
            "b": {"name": "ISP-B", "block": "100.66.0.0/16", "vlan_base": 3230},
        },
    },
    # optional Traffic-Gen container: management on the uplink network (own IP), traffic from the first spoke's LAN
    # Traffic-Gen always comes along (Everything / FortiGates); management IP = uplink IP minus one unless set
    "trafficgen": {"enabled": True, "ip": "", "count": 10},
    "region": {
        "name": "West",
        "bgp_as": 65001,
        "lo_summary": "10.200.1.0/24",
        "multi_vrf": False,                  # False: classic single-VRF design (one LAN per device, no PE-VRF/leak)
        "lan_summary": "10.0.0.0/8",         # single-VRF only (multi-VRF: per VRF)
        "vrfs": [
            {"id": 11, "lan_summary": "10.0.0.0/14", "dia": True},
            {"id": 12, "lan_summary": "10.0.0.0/14", "dia": True},
        ],
    },
    "hubs": [
        _hub("site1-H1", "10.200.1.253", "100.64.1.2", "10.1.0.1/24"),     # wan1 = derived (normalize_wans)
        _hub("site1-H2", "10.200.1.254", "100.64.2.2", "10.2.0.1/24"),
    ],
    "edges": [
        _edge("site1-1", "10.200.1.1", 1),
        _edge("site1-2", "10.200.1.2", 2),
        _edge("site1-3", "10.200.1.3", 3),
    ],
}

_SN = re.compile(r"^FG[A-Z0-9]{3,}\d{4,}$|^FGVM[A-Z0-9]{8,}$", re.I)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,34}$")
_TOKEN = re.compile(r"^[A-Za-z0-9]{10,64}$")


def _ip(v, net=False):
    """Plain IPv4 address (net=False) or, leniently, an IPv4 interface/network such as 10.0.0.0/14 (net=True)."""
    try:
        (ipaddress.IPv4Interface if net else ipaddress.IPv4Address)(str(v).strip())
        return True
    except ValueError:
        return False


def _cidr(v):
    """Host address with prefix, strictly a.b.c.d/nn (1-32). Network/broadcast addresses are refused up to /30."""
    s = str(v).strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}/\d{1,2}", s):
        return False
    try:
        i = ipaddress.IPv4Interface(s)
    except ValueError:
        return False
    n = i.network
    return n.prefixlen >= 31 or i.ip not in (n.network_address, n.broadcast_address)


def split_cidr(v):
    """'10.0.0.5/24' -> ('10.0.0.5', '255.255.255.0')"""
    i = ipaddress.ip_interface(v.strip())
    return str(i.ip), str(i.netmask)


def _gw_ok(cidr, gw):
    try:
        return ipaddress.ip_address(gw.strip()) in ipaddress.ip_interface(cidr.strip()).network
    except ValueError:
        return False


def valid_token(t):
    """FortiFlex tokens are plain alphanumerics; reject anything that could carry CLI syntax."""
    return bool(_TOKEN.match((t or "").strip()))


def validate(t, devices=True, fmg_vm=True):
    """devices=False ('Alleen FortiManager' / 'Alleen templates'): no model devices are created, so the serial number and
    the wan1 gateway (only a per-device variable value) are not needed yet; they are checked when the FortiGates are
    deployed. fmg_vm=False ('Alleen FortiGates' / 'Alleen templates'): an existing FortiManager, its IP is only the API
    and FortiGate management address (no gateway: the tool does not configure that FortiManager's network)."""
    err = []
    normalize_wans(t)
    if not _NAME.match(t.get("adom", "")):
        err.append("ADOM name: 1-35 characters (letters, digits, _ . -), starting with a letter/digit")
    if t.get("os_ver") not in ("7.6", "8.0"):
        err.append("os_ver must be 8.0 (FortiManager 8.0.x) or 7.6 (FortiManager 7.6.x)")
    r = t.get("region", {})
    if _multi(t):
        if not r.get("vrfs"):
            err.append("Multi-VRF: at least 1 VRF needed (or turn multi-VRF off)")
        ids = [v.get("id") for v in r.get("vrfs", [])]
        if len(set(ids)) != len(ids) or any(not isinstance(i, int) or i < 2 or i > 251 for i in ids):
            err.append("VRF ids: unique, integer 2-251 (VRF 1 is reserved as PE-VRF)")
        for v in r.get("vrfs", []):
            if not _ip(v.get("lan_summary", ""), True):
                err.append(f"VRF {v.get('id')}: invalid lan_summary")
    elif not _ip(r.get("lan_summary", ""), True):
        err.append("Single-VRF: invalid LAN summary")
    if len(t.get("hubs", [])) < 1 or len(t.get("edges", [])) < 1:
        err.append("At least 1 hub and 1 branch needed")

    f = t.get("fmgr", {})
    if not fmg_vm:                             # existing FortiManager: only its address is used (API + FortiGate central-management)
        if not (_ip(f.get("ip", "")) or _cidr(f.get("ip", ""))):
            err.append("FortiManager: IP address (a.b.c.d) is required")
    elif not _cidr(f.get("ip", "")):
        err.append("FortiManager: a static IP address as a.b.c.d/nn is required")
    elif fmg_vm and (not _ip(f.get("gateway", "")) or not _gw_ok(f["ip"], f["gateway"])):
        err.append("FortiManager: gateway missing or not in the subnet of the FMG IP")
    if f.get("initial_ip") and not _ip(f["initial_ip"]):
        err.append("FortiManager: invalid initial IP")

    if not t.get("dns_primary"):
        err.append("Primary DNS is required (the FortiFlex licence and FortiGuard need DNS)")
    for k in ("dns_primary", "dns_secondary"):
        if t.get(k) and not _ip(t[k]):
            err.append(f"{k}: invalid IP")
    if not re.fullmatch(r"[a-z ]+", t.get("wan1_access", "")):
        err.append("wan1_access: lower-case letters and spaces only (e.g. 'ping https ssh fgfm')")

    names, sns = set(), set()
    for kind in ("hubs", "edges"):
        for d in t.get(kind, []):
            n = d.get("name", "")
            if not _NAME.match(n):
                err.append(f"{kind}: invalid name '{n}'")
            if n in names:
                err.append(f"Duplicate name '{n}'")
            names.add(n)
            sn = d.get("serial", "").strip()
            if devices and not _SN.match(sn):
                err.append(f"{n}: serial number missing/invalid ('{sn}')")
            if devices and sn and sn.upper() in sns:
                err.append(f"{n}: duplicate serial number")
            sns.add(sn.upper())
            if not _ip(d.get("loopback", "")):
                err.append(f"{n}: invalid loopback address")
            for k, l in enumerate(d.get("lans", [])):
                if l.get("intf") and not _cidr(l.get("ip", "")):
                    err.append(f"{n}: LAN{k+1} invalid IP/prefix")
            if _multi(t) and len(d.get("lans", [])) > len(r.get("vrfs", [])):
                err.append(f"{n}: more LANs than VRFs")
            if _multi(t):                          # the PE-VRF routes back to each LAN (DIA without NAT): no overlap
                nets = [ipaddress.ip_interface(l["ip"]).network for l in d.get("lans", [])
                        if l.get("intf") and _cidr(l.get("ip", ""))]
                if any(a.overlaps(b) for i, a in enumerate(nets) for b in nets[i + 1:]):
                    err.append(f"{n}: LAN subnets overlap; in multi-VRF every LAN of a FortiGate needs its own subnet "
                               "(internet access from the VRFs is routed back per LAN subnet)")
            if not d.get("wan1_intf"):
                err.append(f"{n}: wan1 port (e.g. port1) is required: the ZTP/FMG connection runs over it")
            if kind == "hubs":                     # hubs: wan1 always static (branches dial in to this address)
                if not _cidr(d.get("wan1_ip", "")):
                    err.append(f"{n}: wan1 IP as a.b.c.d/nn required (hub: always static)")
                elif not _ip(d.get("wan1_gw", "")) or not _gw_ok(d["wan1_ip"], d["wan1_gw"]):   # also without devices:
                    err.append(f"{n}: wan1 gateway missing or not in the subnet")          # it is in the hub SD-WAN template
            elif d.get("wan2_intf") and d["wan2_intf"] == d.get("wan1_intf"):
                err.append(f"{n}: wan2 port must be another port than wan1")
            if d.get("initial_ip") and not _ip(d["initial_ip"]):
                err.append(f"{n}: invalid initial IP")
    return err


def _lan_var(k, suffix):
    return f"lan{'' if k == 0 else k + 1}_{suffix}"


def hub_wan1_ip(h):
    """Hub underlay address the branches connect to: the (always static) wan1 address."""
    return split_cidr(h["wan1_ip"])[0] if _cidr(h.get("wan1_ip", "")) else None


HUB_DROP = ("wan2_intf", "wan2_ip", "wan1_hub_ip")         # hubs have one wan
EDGE_DROP = ("wan1_ip", "wan1_gw", "wan1_hub_ip", "wan2_ip")  # branch wans are always DHCP


def password_problems(pw):
    """What a password lacks to be strong: 8+ characters, upper and lower case, a digit and a special character.
    The same rule is in the GUI (static/app.js pwProblems)."""
    pw = pw or ""
    checks = (("at least 8 characters", len(pw) >= 8), ("an uppercase letter", re.search(r"[A-Z]", pw)),
              ("a lowercase letter", re.search(r"[a-z]", pw)), ("a digit", re.search(r"[0-9]", pw)),
              ("a special character", re.search(r"[^A-Za-z0-9]", pw)))
    return [what for what, ok in checks if not ok]


def _link_net(t, seg, n):
    """The n-th /24 (1-based) of an ISP block, or None when the block is invalid / too small."""
    try:
        b = ipaddress.ip_network(str(((t.get("wanemu") or {}).get("segments") or {}).get(seg, {}).get("block", "")).strip())
    except ValueError:
        return None
    if b.version != 4 or b.prefixlen > 23 or n >= 2 ** (24 - b.prefixlen):
        return None
    return ipaddress.ip_network((int(b.network_address) + n * 256, 24))


def wan_links(t):
    """Every FortiGate WAN as a link with its own /24 and VLAN, in a fixed order: hubs (wan1), then each spoke wan1
    and wan2. [{id, name, seg, n, subnet, gw, ip, vlan}]; a link whose block is invalid is left out."""
    seg = ((t.get("wanemu") or {}).get("segments") or {})
    todo = [(h["name"], "hub", n, "wan1") for n, h in enumerate(t.get("hubs", []), start=1)]
    for n, e in enumerate(t.get("edges", []), start=1):
        todo.append((e["name"], "a", n, "wan1"))
        if e.get("wan2_intf"):
            todo.append((e["name"], "b", n, "wan2"))
    out = []
    for name, s, n, w in todo:
        net = _link_net(t, s, n)
        vb = seg.get(s, {}).get("vlan_base")
        if net is None or not isinstance(vb, int):
            continue
        out.append({"id": f"{name}-{w}", "name": name, "seg": s, "n": n, "subnet": str(net),
                    "gw": str(net.network_address + 1), "ip": str(net.network_address + 2), "vlan": vb + n})
    return out


def normalize_wans(t):
    """Enforce the WAN design on any input (GUI, API, old CSV): hub = wan1 static only, branch = wan1/wan2 DHCP. The hub
    wan1 address is derived: .2 of its own /24 in ISP-HUB, gateway .1 (the wan-emu)."""
    for n, h in enumerate(t.get("hubs", []), start=1):
        h["wan1_mode"] = "static"
        for k in HUB_DROP:
            h.pop(k, None)
        net = _link_net(t, "hub", n)
        if net is not None:
            h["wan1_ip"], h["wan1_gw"] = f"{net.network_address + 2}/24", str(net.network_address + 1)
    for e in t.get("edges", []):
        e["wan1_mode"] = "dhcp"
        for k in EDGE_DROP:
            e.pop(k, None)
    for d in t.get("hubs", []) + t.get("edges", []):
        lans = [l for j, l in enumerate(d.get("lans") or []) if j == 0 or l.get("intf")]   # LAN2+ without a port: none
        d["lans"] = lans if _multi(t) else lans[:1]    # single-VRF: one LAN per device (LAN2 only exists per VRF)
    return t


def _multi(t):
    return bool(t.get("region", {}).get("multi_vrf", True))


def pe_vrf(t):
    """VRF that carries the underlay (wan1/FMG tunnel): 1 in multi-VRF, the default VRF (0) otherwise."""
    return 1 if _multi(t) else 0


def build(t):
    """Return (project_template_text, inventory_dict)."""
    normalize_wans(t)
    r = t["region"]
    reg_name = r["name"]
    multi = _multi(t)
    vrfs = r["vrfs"] if multi else []
    regions = {reg_name: {"as": str(r["bgp_as"]), "lo_summary": r["lo_summary"], "hubs": [h["name"] for h in t["hubs"]]}}
    if multi:
        regions[reg_name]["vrfs"] = [{"id": v["id"], "lan_summary": v["lan_summary"]} for v in vrfs]
    access = json.dumps(t.get("wan1_access", "ping https ssh fgfm").split())
    # 'access' keeps SSH/HTTPS/fgfm on wan1, otherwise the template resets allowaccess to 'ping'.
    # Hub: one wan, static (wan1_addr = a.b.c.d/nn). Branch: wan1 + wan2, both DHCP and both ol_type WAN1, so the
    # Orchestrator builds a tunnel from EVERY branch wan to wan1 of every hub (<hub>_WAN1 and <hub>_WAN1_2).
    hub_wan = [f"{{'name': wan1_intf, 'role': 'wan', 'ol_type': 'WAN1', 'ip': wan1_addr, 'dia': true, 'access': {access}}}"]
    edge_wan = [f"{{'name': wan1_intf, 'role': 'wan', 'ol_type': 'WAN1', 'ip': 'dhcp', 'dia': true, 'access': {access}}}",
                "{'name': wan2_intf, 'role': 'wan', 'ol_type': 'WAN1', 'ip': 'dhcp', 'dia': true}"]
    ifaces = []
    for k, v in enumerate(vrfs):
        dia = ", 'allow_dia': true" if v.get("dia") else ""
        ifaces.append(f"{{'name': {_lan_var(k,'intf')}, 'role': 'lan', 'vrf': {v['id']}{dia}, "
                      f"'ip': {_lan_var(k,'ip')}}}")
    if not multi:                                   # single VRF: exactly one LAN, in the default VRF
        ifaces.append(f"{{'name': {_lan_var(0,'intf')}, 'role': 'lan', 'ip': {_lan_var(0,'ip')}}}")
    hubs = {}
    for n, h in enumerate(t["hubs"], start=1):
        ov = {}
        if h.get("wan1_intf") and hub_wan1_ip(h):
            ov["WAN1"] = {"wan_ip": hub_wan1_ip(h), "network_id": str(n * 10 + 1),
                         "hub2hub": bool(t.get("hub2hub"))}    # 'Hub-to-hub tunnels': H1H2_WAN1 + zone hub2hub_overlay
        hubs[h["name"]] = {"lo_bgp": h["loopback"], "overlays": ov}
    parts = [
        "{% set cert_auth = false %}",
        "{% set advpn2 = true %}",
        "{% set dynamic_bgp = true %}",
    ]
    if t.get("hub2hub"):
        parts.append("{% set intrareg_hub2hub = true %}")
    if t.get("psk"):
        parts.append("{% set psk = " + json.dumps(t["psk"]) + " %}")
    parts += [
        "{% set lo_summary = " + json.dumps(t["lo_summary"]) + " %}",
    ] + (["{% set lan_summary = " + json.dumps(r.get("lan_summary", "10.0.0.0/8")) + " %}"] if not multi else []) + [
        "{% set regions = " + json.dumps(regions, indent=1) + " %}",
        "{% set profiles = {'Hub': {'interfaces': [\n  " + ",\n  ".join(hub_wan + ifaces) + "\n]},\n"
        " 'Edge': {'interfaces': [\n  " + ",\n  ".join(edge_wan + ifaces) + "\n]}} %}",
        "{% set hubs = " + json.dumps(hubs, indent=1) + " %}",
    ]
    project = "\n".join(parts) + "\n"

    def meta(d, hub):
        m = {"hostname": d["name"], "loopback": d["loopback"], "profile": "Hub" if hub else "Edge", "region": reg_name}
        if hub:
            m["wan1_addr"] = d["wan1_ip"].strip()
        for k in ("wan1_intf",) if hub else ("wan1_intf", "wan2_intf"):
            if d.get(k):
                m[k] = d[k]
        for k, l in enumerate(d.get("lans", [])[:None if multi else 1]):
            if l.get("intf"):
                m[_lan_var(k, "intf")] = l["intf"]
                m[_lan_var(k, "ip")] = l["ip"]
        return m

    inv = {"Hub": {h["name"]: meta(h, True) for h in t["hubs"]},
           "Edge": {e["name"]: meta(e, False) for e in t["edges"]}}
    return project, inv


def underlay_fragment(t, d):
    """Extra CLI appended to a device's rendered config (things the Orchestrator does not emit).

    The Orchestrator moves wan1 into the PE-VRF (1) and installs no static default route. For a
    static wan1 we add the default route (id 1 = same id as the ZTP bootstrap route, so it is
    updated in place) and make the FortiManager tunnel follow the VRF move."""
    pe = pe_vrf(t)
    out = [f"# ZTP underlay (fgt-sdwan-provision) - {d['name']}"]
    if d.get("wan1_mode") == "static":
        out += ["config router static", "  edit 1", f"    set gateway {d['wan1_gw'].strip()}",
                f"    set device \"{d['wan1_intf']}\"", *([f"    set vrf {pe}"] if pe else []), "  next", "end"]
    out += ["config system dns", f"  set primary {t['dns_primary']}"]
    if t.get("dns_secondary"):
        out.append(f"  set secondary {t['dns_secondary']}")
    out += ["end",
            "config system central-management", "  set type fortimanager",
            f"  set fmg \"{split_cidr(t['fmgr']['ip'])[0]}\"", *([f"  set vrf-select {pe}"] if pe else []), "end"]
    return "\n".join(out) + "\n"


def append_fragment(config, fragment):
    marker = "execute batch end"
    head, sep, tail = config.rpartition(marker)
    if not sep:
        return config + fragment
    return head + fragment + sep + tail


def _deep(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep(base[k], v)
        elif v is not None:
            base[k] = v
    return base


# Not configurable (always the same in our deployments). Enforced here so no client can override them.
FIXED = {"platform": "FortiGate-VM64", "dns_primary": "1.1.1.1", "dns_secondary": "8.8.8.8",
         "wan1_access": "ping https ssh fgfm", "lo_summary": "10.200.0.0/14",
         "region": {"name": "West", "bgp_as": 65001, "lo_summary": "10.200.1.0/24"},
         "wanemu": {"template": DEFAULTS["wanemu"]["template"]},   # CT template: not a user choice
         "trafficgen": {"count": 10}}


SEG_KEYS = ("hub", "a", "b")
MAX_WAN_LINKS = 31                                       # Proxmox: 32 NICs per container, net0 is the uplink


def old_segments(user):
    """True when the input still has the old layout (one shared subnet per ISP: subnet/gw instead of block)."""
    seg = ((user or {}).get("wanemu") or {}).get("segments") or {}
    return any(isinstance(seg.get(k), dict) and ("subnet" in seg[k] or "gw" in seg[k]) for k in SEG_KEYS)


def merged(user):
    t = copy.deepcopy(DEFAULTS)
    user = copy.deepcopy(user or {})
    seg = ((user.get("wanemu") or {}).get("segments")) if isinstance(user.get("wanemu"), dict) else None
    if isinstance(seg, dict):                            # old layout (subnet/gw per ISP): back to the new defaults
        for k in SEG_KEYS:
            if isinstance(seg.get(k), dict) and ("subnet" in seg[k] or "gw" in seg[k]):
                seg[k] = {"name": seg[k].get("name") or DEFAULTS["wanemu"]["segments"][k]["name"]}
    for k, v in user.items():                            # never share lists/dicts with the caller (or DEFAULTS)
        if k in ("pve", "wanemu", "trafficgen") and isinstance(v, dict):
            _deep(t[k], v)
        elif v is not None:
            t[k] = v
    _deep(t, copy.deepcopy(FIXED))
    tg = t.get("trafficgen") or {}
    if not str(tg.get("ip") or "").strip():                 # derived: the address below the wan-emu uplink address
        try:
            up = ipaddress.ip_interface(str(t["wanemu"]["uplink"].get("ip") or "").strip())
            gw = str(t["wanemu"]["uplink"].get("gw") or "").strip()
            if up.network.prefixlen < 31:
                for cand in (up.ip - 1, up.ip + 1):            # not the network, broadcast or gateway address
                    if cand in up.network and cand not in (up.network.network_address, up.network.broadcast_address) \
                            and str(cand) != gw:
                        tg["ip"] = f"{cand}/{up.network.prefixlen}"
                        break
        except ValueError:
            pass
    pre = str(t["pve"].get("name_prefix") or "").strip()
    t["pve"]["name_prefix"] = pre + "-" if pre and not pre.endswith("-") else pre
    return normalize_wans(t)


def pve_name(t, name):
    """Proxmox name of a VM/CT of this deployment: the name prefix + the name (device names do not change)."""
    return ((t.get("pve") or {}).get("name_prefix") or "") + name


def validate_wanemu(t):
    """WAN emulator: ISP blocks and VLAN ranges (one /24 + VLAN per WAN), uplink, port forwards."""
    w, err = t.get("wanemu", {}), []
    seg, up = w.get("segments", {}), w.get("uplink", {})
    n_fgt = len(t.get("hubs", [])) + len(t.get("edges", []))
    nets = {}
    for lbl, b in (("bridge", w.get("bridge")), ("uplink bridge", up.get("bridge") or w.get("bridge"))):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", str(b or "")):
            err.append(f"WAN emulation: {lbl} missing or invalid")
    count = {"hub": len(t.get("hubs", [])), "a": len(t.get("edges", [])),
             "b": len(t.get("edges", []))}                     # link numbers 1..count per ISP (wan2: spoke number)
    isp_vlans = {}
    for key in ("hub", "a", "b"):
        g = seg.get(key, {})
        name = g.get("name", key)
        try:
            n = ipaddress.ip_network(str(g.get("block", "")).strip())
            ok = n.version == 4 and n.prefixlen <= 23 and count[key] < 2 ** (24 - n.prefixlen)
        except ValueError:
            ok = False
        if not ok:
            err.append(f"WAN emulation {name}: block must be an IPv4 network like 100.65.0.0/16 with a /24 per "
                       f"FortiGate ({count[key]} needed, numbered from 1)")
        else:
            nets[name] = n
        vb = g.get("vlan_base")
        if not isinstance(vb, int) or vb < 1 or vb + max(count[key], 1) > 4094:
            err.append(f"WAN emulation {name}: VLAN base must be 1-{4094 - max(count[key], 1)}")
        else:
            isp_vlans[name] = range(vb + 1, vb + 1 + count[key])
    if len(wan_links(t)) > MAX_WAN_LINKS:
        err.append(f"WAN emulation: {len(wan_links(t))} WAN links, at most {MAX_WAN_LINKS} (one network card each on "
                   "the wan-emu container; Proxmox allows 32 including the uplink)")
    try:
        ui = ipaddress.ip_interface(str(up.get("ip", "")).strip())
        if ui.network.prefixlen == 32 or not _ip(up.get("gw", "")) or ipaddress.ip_address(up["gw"].strip()) not in ui.network:
            err.append("WAN emulation uplink: gateway missing or not in the subnet of the uplink IP")
        nets["uplink"] = ui.network
    except ValueError:
        err.append("WAN emulation uplink: IP as a.b.c.d/nn is required (the address of wan-emu in your lab network)")
    names = list(nets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if nets[a].overlaps(nets[b]):
                err.append(f"WAN emulation: {a} and {b} overlap")
    for key in ("lan_vlan_base", "lan2_vlan_base"):            # LAN VLANs base+1 .. base+n must stay clear
        b = w.get(key)
        rng = range(b + 1, b + 1 + n_fgt) if isinstance(b, int) else range(0)
        if not isinstance(b, int) or b < 1 or b + n_fgt > 4094:
            err.append(f"WAN emulation: LAN VLAN base {key} must be 1-{4094 - n_fgt}")
        elif up.get("vlan") in rng or any(set(rng) & set(r) for r in isp_vlans.values()):
            err.append(f"WAN emulation: LAN VLANs {rng.start}-{rng.stop - 1} clash with the uplink or ISP VLANs")
    if isinstance(w.get("lan_vlan_base"), int) and isinstance(w.get("lan2_vlan_base"), int) and \
            abs(w["lan_vlan_base"] - w["lan2_vlan_base"]) < n_fgt:
        err.append("WAN emulation: the LAN VLANs for LAN1 and LAN2 overlap")
    names = list(isp_vlans)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if set(isp_vlans[a]) & set(isp_vlans[b]):
                err.append(f"WAN emulation: the VLANs of {a} and {b} overlap ({isp_vlans[a].start}-"
                           f"{isp_vlans[a].stop - 1} / {isp_vlans[b].start}-{isp_vlans[b].stop - 1})")
    uv = up.get("vlan")
    if uv not in (None, "") and (not isinstance(uv, int) or not 1 <= uv <= 4094):
        err.append("WAN emulation: uplink VLAN must be 1-4094")
    elif uv not in (None, "") and any(uv in r for r in isp_vlans.values()):
        err.append(f"WAN emulation: uplink VLAN {uv} is one of the ISP VLANs")
    ranges = []
    for key, lbl in (("fwd_https_base", "HTTPS"), ("fwd_ssh_base", "SSH")):
        b = w.get(key)
        if not isinstance(b, int) or not 1024 <= b <= 65535 - n_fgt:
            err.append(f"WAN emulation: port forward {lbl} base must be 1024-{65535 - n_fgt}")
        else:
            ranges.append((lbl, b, b + n_fgt - 1))
    if len(ranges) == 2 and not (ranges[0][2] < ranges[1][1] or ranges[1][2] < ranges[0][1]):
        err.append("WAN emulation: the port ranges for HTTPS and SSH overlap")
    for lbl, lo, hi in ranges:
        if lo <= w.get("agent_port", 8090) <= hi:
            err.append(f"WAN emulation: {lbl} port range {lo}-{hi} contains the agent port {w.get('agent_port', 8090)}")
    return err


def validate_pve(t, wanemu=True):
    """Extra checks when VMs are created on Proxmox (day-0). wanemu=False: no FortiGate-VMs in this run (e.g. only the
    FortiManager), so the WAN emulator - which comes with every FortiGate-VM deployment - is not checked."""
    c, err = t.get("pve", {}), (validate_wanemu(t) if wanemu else [])
    if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", c.get("url", "")):
        err.append("Proxmox: URL as https://host:8006 is required")
    if not re.match(r"^[^\s!]+@[^\s!]+![^\s!]+$", c.get("token_id", "")):
        err.append("Proxmox: token ID as user@realm!name is required")
    for k, lbl in (("node", "node"), ("disk_storage", "disk storage"), ("import_storage", "import storage"),
                   ("iso_storage", "ISO storage")):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", c.get(k, "")):
            err.append(f"Proxmox: {lbl} missing or invalid")
    if c.get("license_mode") not in ("mime", "cli"):
        err.append("Proxmox: licence mode must be 'mime' or 'cli'")
    br = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    bridges = [c.get("default_bridge", "")] + [p.get("bridge") for p in c.get("ports", {}).values() if p.get("bridge")]
    bridges.append(c.get("fmg", {}).get("bridge") or c.get("default_bridge", ""))
    if any(not br.match(b or "") for b in bridges):
        err.append("Proxmox: invalid bridge name")
    vlans = [p.get("vlan") for p in c.get("ports", {}).values()] + [c.get("fmg", {}).get("vlan")]
    if any(v not in (None, "") and not (isinstance(v, int) and 1 <= v <= 4094) for v in vlans):
        err.append("Proxmox: VLAN id must be 1-4094")
    fm, fg = c.get("fmg", {}), c.get("fgt", {})
    for lbl, d, keys in (("FortiManager", fm, ("cores", "memory_mb", "data_disk_gb")), ("FortiGate", fg, ("cores", "memory_mb"))):
        for k in keys:
            if not isinstance(d.get(k), int) or d[k] < 1:
                err.append(f"Proxmox: {lbl} {k} must be a positive integer")
    pre = c.get("name_prefix") or ""
    if pre and not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,20}$", pre):
        err.append("Proxmox: Name prefix may only contain letters, digits and '-' (max 20, e.g. lab1)")
    fv = c.get("first_vmid")
    if fv not in (None, "") and not (isinstance(fv, int) and 100 <= fv <= 999999000):
        err.append("Proxmox: First VM ID must be 100-999999000 (or empty = automatic)")
    if isinstance(fg.get("memory_mb"), int) and fg["memory_mb"] < 1024:
        err.append("Proxmox: FortiGate memory at least 1024 MB")
    return err
