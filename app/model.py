"""Topology model: defaults, validation and translation to Jinja Orchestrator inputs."""
import copy
import ipaddress
import json
import re

DEFAULT_INITIAL_IP = "192.168.1.99"   # factory default of FortiGate-VM / FortiManager-VM port1


# WAN design (user decision): a hub has ONE wan (wan1), always static; a branch has wan1 + optional wan2, both
# always DHCP. Every branch wan builds a tunnel to wan1 of every hub. `wan1_mode` stays as an internal key (other
# code reads it) but is forced by normalize_wans(): hub = static, branch = dhcp.
def _hub(name, lo, wan1, lan):
    return {"name": name, "serial": "", "loopback": lo,
            "wan1_intf": "port1", "wan1_mode": "static", "wan1_ip": f"{wan1}/29", "wan1_gw": "",
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
        "ports": {f"port{i}": {"bridge": "", "vlan": None} for i in range(1, 9)},
        "fmg": {"bridge": "", "vlan": None, "cores": 4, "memory_mb": 16384, "data_disk_gb": 100},
        "fgt": {"cores": 2, "memory_mb": 2048},
    },
    "region": {
        "name": "West",
        "bgp_as": 65001,
        "lo_summary": "10.200.1.0/24",
        "multi_vrf": True,                   # False: classic single-VRF design (one LAN per device, no PE-VRF/leak)
        "lan_summary": "10.0.0.0/8",         # single-VRF only (multi-VRF: per VRF)
        "vrfs": [
            {"id": 11, "lan_summary": "10.0.0.0/14", "dia": True},
            {"id": 12, "lan_summary": "10.0.0.0/14", "dia": False},
        ],
    },
    "hubs": [
        _hub("site1-H1", "10.200.1.253", "100.64.1.1", "10.1.0.1/24"),
        _hub("site1-H2", "10.200.1.254", "100.64.2.1", "10.2.0.1/24"),
    ],
    "edges": [
        _edge("site1-1", "10.200.1.1", 1),
        _edge("site1-2", "10.200.1.2", 2, wan2=False),
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
        err.append("ADOM-naam: 1-35 tekens (letters, cijfers, _ . -), begint met letter/cijfer")
    if t.get("os_ver") not in ("7.6", "8.0"):
        err.append("os_ver moet 8.0 (FortiManager 8.0.x) of 7.6 (FortiManager 7.6.x) zijn")
    r = t.get("region", {})
    if _multi(t):
        if not r.get("vrfs"):
            err.append("Multi-VRF: minstens 1 VRF nodig (of zet multi-VRF uit)")
        ids = [v.get("id") for v in r.get("vrfs", [])]
        if len(set(ids)) != len(ids) or any(not isinstance(i, int) or i < 2 or i > 251 for i in ids):
            err.append("VRF-id's: uniek, geheel getal 2-251 (VRF 1 is gereserveerd als PE-VRF)")
        for v in r.get("vrfs", []):
            if not _ip(v.get("lan_summary", ""), True):
                err.append(f"VRF {v.get('id')}: ongeldige lan_summary")
    elif not _ip(r.get("lan_summary", ""), True):
        err.append("Single-VRF: ongeldige LAN-summary")
    if len(t.get("hubs", [])) < 1 or len(t.get("edges", [])) < 1:
        err.append("Minstens 1 hub en 1 branch nodig")

    f = t.get("fmgr", {})
    if not fmg_vm:                             # existing FortiManager: only its address is used (API + FortiGate central-management)
        if not (_ip(f.get("ip", "")) or _cidr(f.get("ip", ""))):
            err.append("FortiManager: IP-adres (a.b.c.d) is verplicht")
    elif not _cidr(f.get("ip", "")):
        err.append("FortiManager: vast IP-adres als a.b.c.d/nn is verplicht")
    elif fmg_vm and (not _ip(f.get("gateway", "")) or not _gw_ok(f["ip"], f["gateway"])):
        err.append("FortiManager: gateway ontbreekt of ligt niet in het subnet van het FMG-IP")
    if f.get("initial_ip") and not _ip(f["initial_ip"]):
        err.append("FortiManager: ongeldig initieel IP")

    if not t.get("dns_primary"):
        err.append("DNS primair is verplicht (FortiFlex-licentie en FortiGuard hebben DNS nodig)")
    for k in ("dns_primary", "dns_secondary"):
        if t.get(k) and not _ip(t[k]):
            err.append(f"{k}: ongeldig IP")
    if not re.fullmatch(r"[a-z ]+", t.get("wan1_access", "")):
        err.append("wan1_access: alleen kleine letters en spaties (bv. 'ping https ssh fgfm')")

    names, sns = set(), set()
    for kind in ("hubs", "edges"):
        for d in t.get(kind, []):
            n = d.get("name", "")
            if not _NAME.match(n):
                err.append(f"{kind}: ongeldige naam '{n}'")
            if n in names:
                err.append(f"Dubbele naam '{n}'")
            names.add(n)
            sn = d.get("serial", "").strip()
            if devices and not _SN.match(sn):
                err.append(f"{n}: serienummer ontbreekt/ongeldig ('{sn}')")
            if devices and sn and sn.upper() in sns:
                err.append(f"{n}: dubbel serienummer")
            sns.add(sn.upper())
            if not _ip(d.get("loopback", "")):
                err.append(f"{n}: ongeldig loopback-adres")
            for k, l in enumerate(d.get("lans", [])):
                if l.get("intf") and not _cidr(l.get("ip", "")):
                    err.append(f"{n}: LAN{k+1} ongeldig IP/prefix")
            if _multi(t) and len(d.get("lans", [])) > len(r.get("vrfs", [])):
                err.append(f"{n}: meer LAN's dan VRF's")
            if not d.get("wan1_intf"):
                err.append(f"{n}: wan1-poort (bv. port1) is verplicht: ZTP/FMG-verbinding loopt hierover")
            if kind == "hubs":                     # hubs: wan1 always static (branches dial in to this address)
                if not _cidr(d.get("wan1_ip", "")):
                    err.append(f"{n}: wan1 IP als a.b.c.d/nn verplicht (hub: altijd statisch)")
                elif not _ip(d.get("wan1_gw", "")) or not _gw_ok(d["wan1_ip"], d["wan1_gw"]):   # also without devices:
                    err.append(f"{n}: wan1 gateway ontbreekt of ligt niet in het subnet")          # it is in the hub SD-WAN template
            elif d.get("wan2_intf") and d["wan2_intf"] == d.get("wan1_intf"):
                err.append(f"{n}: wan2-poort moet een andere poort zijn dan wan1")
            if d.get("initial_ip") and not _ip(d["initial_ip"]):
                err.append(f"{n}: ongeldig initieel IP")
    return err


def _lan_var(k, suffix):
    return f"lan{'' if k == 0 else k + 1}_{suffix}"


def hub_wan1_ip(h):
    """Hub underlay address the branches connect to: the (always static) wan1 address."""
    return split_cidr(h["wan1_ip"])[0] if _cidr(h.get("wan1_ip", "")) else None


HUB_DROP = ("wan2_intf", "wan2_ip", "wan1_hub_ip")         # hubs have one wan
EDGE_DROP = ("wan1_ip", "wan1_gw", "wan1_hub_ip", "wan2_ip")  # branch wans are always DHCP


def normalize_wans(t):
    """Enforce the WAN design on any input (GUI, API, old CSV): hub = wan1 static only, branch = wan1/wan2 DHCP."""
    for h in t.get("hubs", []):
        h["wan1_mode"] = "static"
        for k in HUB_DROP:
            h.pop(k, None)
    for e in t.get("edges", []):
        e["wan1_mode"] = "dhcp"
        for k in EDGE_DROP:
            e.pop(k, None)
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
            ov["WAN1"] = {"wan_ip": hub_wan1_ip(h), "network_id": str(n * 10 + 1), "hub2hub": False}
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
         "region": {"name": "West", "bgp_as": 65001, "lo_summary": "10.200.1.0/24"}}


def merged(user):
    t = copy.deepcopy(DEFAULTS)
    for k, v in (user or {}).items():
        if k == "pve" and isinstance(v, dict):
            _deep(t["pve"], v)
        elif v is not None:
            t[k] = v
    _deep(t, copy.deepcopy(FIXED))
    return normalize_wans(t)


def validate_pve(t):
    """Extra checks when VMs are created on Proxmox (day-0)."""
    c, err = t.get("pve", {}), []
    if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", c.get("url", "")):
        err.append("Proxmox: URL als https://host:8006 is verplicht")
    if not re.match(r"^[^\s!]+@[^\s!]+![^\s!]+$", c.get("token_id", "")):
        err.append("Proxmox: token-ID als user@realm!naam is verplicht")
    for k, lbl in (("node", "node"), ("disk_storage", "disk-storage"), ("import_storage", "import-storage"),
                   ("iso_storage", "ISO-storage")):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", c.get(k, "")):
            err.append(f"Proxmox: {lbl} ontbreekt of is ongeldig")
    if c.get("license_mode") not in ("mime", "cli"):
        err.append("Proxmox: licentiemodus moet 'mime' of 'cli' zijn")
    br = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    bridges = [c.get("default_bridge", "")] + [p.get("bridge") for p in c.get("ports", {}).values() if p.get("bridge")]
    bridges.append(c.get("fmg", {}).get("bridge") or c.get("default_bridge", ""))
    if any(not br.match(b or "") for b in bridges):
        err.append("Proxmox: ongeldige bridge-naam")
    vlans = [p.get("vlan") for p in c.get("ports", {}).values()] + [c.get("fmg", {}).get("vlan")]
    if any(v not in (None, "") and not (isinstance(v, int) and 1 <= v <= 4094) for v in vlans):
        err.append("Proxmox: VLAN-id moet 1-4094 zijn")
    fm, fg = c.get("fmg", {}), c.get("fgt", {})
    for lbl, d, keys in (("FortiManager", fm, ("cores", "memory_mb", "data_disk_gb")), ("FortiGate", fg, ("cores", "memory_mb"))):
        for k in keys:
            if not isinstance(d.get(k), int) or d[k] < 1:
                err.append(f"Proxmox: {lbl} {k} moet een positief geheel getal zijn")
    if isinstance(fg.get("memory_mb"), int) and fg["memory_mb"] < 1024:
        err.append("Proxmox: FortiGate-geheugen minimaal 1024 MB")
    return err
