"""Topology model: defaults, validation and translation to Jinja Orchestrator inputs."""
import copy
import ipaddress
import json
import re

DEFAULTS = {
    "adom": "SDWAN-MV",
    "os_ver": "8.0",
    "mr": 0,
    "platform": "FortiGate-VM64",
    "psk": "",
    "hub2hub": False,
    "lo_summary": "10.200.0.0/14",
    "region": {
        "name": "West",
        "bgp_as": 65001,
        "lo_summary": "10.200.1.0/24",
        "vrfs": [
            {"id": 11, "lan_summary": "10.0.0.0/14", "dia": True},
            {"id": 12, "lan_summary": "10.0.0.0/14", "dia": False},
        ],
    },
    "hubs": [
        {"name": "site1-H1", "serial": "", "loopback": "10.200.1.253",
         "isp1_intf": "port1", "isp2_intf": "port2", "mpls_intf": "port4",
         "isp1_ip": "100.64.1.1", "isp2_ip": "100.64.1.9", "mpls_ip": "172.16.1.5",
         "lans": [{"intf": "port5", "ip": "10.1.0.1/24"}]},
        {"name": "site1-H2", "serial": "", "loopback": "10.200.1.254",
         "isp1_intf": "port1", "isp2_intf": "port2", "mpls_intf": "port4",
         "isp1_ip": "100.64.2.1", "isp2_ip": "100.64.2.9", "mpls_ip": "172.16.2.5",
         "lans": [{"intf": "port5", "ip": "10.2.0.1/24"}]},
    ],
    "edges": [
        {"name": "site1-1", "serial": "", "loopback": "10.200.1.1",
         "isp1_intf": "port1", "isp2_intf": "port2", "mpls_intf": "port4",
         "lans": [{"intf": "port5", "ip": "10.0.1.1/24"}, {"intf": "port6", "ip": "10.0.101.1/24"}]},
        {"name": "site1-2", "serial": "", "loopback": "10.200.1.2",
         "isp1_intf": "port1", "isp2_intf": "", "mpls_intf": "port4",
         "lans": [{"intf": "port5", "ip": "10.0.2.1/24"}, {"intf": "port6", "ip": "10.0.102.1/24"}]},
        {"name": "site1-3", "serial": "", "loopback": "10.200.1.3",
         "isp1_intf": "port1", "isp2_intf": "port2", "mpls_intf": "port4",
         "lans": [{"intf": "port5", "ip": "10.0.3.1/24"}, {"intf": "port6", "ip": "10.0.103.1/24"}]},
    ],
}

_SN = re.compile(r"^FG[A-Z0-9]{3,}\d{4,}$|^FGVM[A-Z0-9]{8,}$", re.I)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,34}$")


def _ip(v, net=False):
    try:
        (ipaddress.ip_interface if net else ipaddress.ip_address)(v)
        return True
    except ValueError:
        return False


def validate(t):
    err = []
    if not _NAME.match(t.get("adom", "")):
        err.append("ADOM-naam: 1-35 tekens (letters, cijfers, _ . -), begint met letter/cijfer")
    if t.get("os_ver") != "8.0":
        err.append("os_ver moet 8.0 zijn voor FortiManager 8.0.x")
    r = t.get("region", {})
    if not r.get("vrfs"):
        err.append("Minstens 1 VRF nodig")
    ids = [v.get("id") for v in r.get("vrfs", [])]
    if len(set(ids)) != len(ids) or any(not isinstance(i, int) or i < 2 or i > 251 for i in ids):
        err.append("VRF-id's: uniek, geheel getal 2-251 (VRF 1 is gereserveerd als PE-VRF)")
    for v in r.get("vrfs", []):
        if not _ip(v.get("lan_summary", ""), True):
            err.append(f"VRF {v.get('id')}: ongeldige lan_summary")
    if len(t.get("hubs", [])) < 1 or len(t.get("edges", [])) < 1:
        err.append("Minstens 1 hub en 1 branch nodig")
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
            if not _SN.match(sn):
                err.append(f"{n}: serienummer ontbreekt/ongeldig ('{sn}')")
            if sn and sn.upper() in sns:
                err.append(f"{n}: dubbel serienummer")
            sns.add(sn.upper())
            if not _ip(d.get("loopback", "")):
                err.append(f"{n}: ongeldig loopback-adres")
            for k, l in enumerate(d.get("lans", [])):
                if l.get("intf") and not _ip(l.get("ip", ""), True):
                    err.append(f"{n}: LAN{k+1} ongeldig IP/prefix")
            if len(d.get("lans", [])) > len(r.get("vrfs", [])):
                err.append(f"{n}: meer LAN's dan VRF's")
            if not d.get("isp1_intf") and not d.get("isp2_intf") and not d.get("mpls_intf"):
                err.append(f"{n}: geen WAN-interface")
    for h in t.get("hubs", []):
        for k in ("isp1", "isp2", "mpls"):
            if h.get(f"{k}_intf") and not _ip(h.get(f"{k}_ip", "")):
                err.append(f"{h.get('name')}: {k}_ip (hub-underlay IP) ongeldig")
    return err


def _lan_var(k, suffix):
    return f"lan{'' if k == 0 else k + 1}_{suffix}"


def build(t):
    """Return (project_template_text, inventory_dict)."""
    r = t["region"]
    reg_name = r["name"]
    vrfs = r["vrfs"]
    regions = {reg_name: {
        "as": str(r["bgp_as"]), "lo_summary": r["lo_summary"],
        "vrfs": [{"id": v["id"], "lan_summary": v["lan_summary"]} for v in vrfs],
        "hubs": [h["name"] for h in t["hubs"]],
    }}
    ifaces = [
        "{'name': isp1_intf, 'role': 'wan', 'ol_type': 'ISP1', 'ip': 'dhcp', 'dia': true}",
        "{'name': isp2_intf, 'role': 'wan', 'ol_type': 'ISP2', 'ip': 'dhcp', 'dia': true}",
        "{'name': mpls_intf, 'role': 'wan', 'ol_type': 'MPLS', 'ip': 'dhcp', 'transport_group': 2}",
    ]
    for k, v in enumerate(vrfs):
        dia = ", 'allow_dia': true" if v.get("dia") else ""
        ifaces.append(f"{{'name': {_lan_var(k,'intf')}, 'role': 'lan', 'vrf': {v['id']}{dia}, "
                      f"'ip': {_lan_var(k,'ip')}}}")
    hubs = {}
    for n, h in enumerate(t["hubs"], start=1):
        ov = {}
        for j, (ol, key) in enumerate((("ISP1", "isp1"), ("ISP2", "isp2"), ("MPLS", "mpls"))):
            if h.get(f"{key}_intf") and h.get(f"{key}_ip"):
                o = {"wan_ip": h[f"{key}_ip"], "network_id": str(n * 10 + j + 1)}
                if ol != "MPLS":
                    o["hub2hub"] = False
                ov[ol] = o
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
        "{% set regions = " + json.dumps(regions, indent=1) + " %}",
        "{% set profiles = {'OneSizeFitsAll': {'interfaces': [\n  " + ",\n  ".join(ifaces) + "\n]}} %}",
        "{% set hubs = " + json.dumps(hubs, indent=1) + " %}",
    ]
    project = "\n".join(parts) + "\n"

    def meta(d):
        m = {"hostname": d["name"], "loopback": d["loopback"], "profile": "OneSizeFitsAll", "region": reg_name}
        for k in ("isp1_intf", "isp2_intf", "mpls_intf"):
            if d.get(k):
                m[k] = d[k]
        for k, l in enumerate(d.get("lans", [])):
            if l.get("intf"):
                m[_lan_var(k, "intf")] = l["intf"]
                m[_lan_var(k, "ip")] = l["ip"]
        return m

    inv = {"Hub": {h["name"]: meta(h) for h in t["hubs"]},
           "Edge": {e["name"]: meta(e) for e in t["edges"]}}
    return project, inv


def merged(user):
    t = copy.deepcopy(DEFAULTS)
    t.update({k: v for k, v in (user or {}).items() if v is not None})
    return t
