"""WAN-emulator: everything derived from the topology (pure functions, no I/O).

Segments: 'hub' = ISP-HUB (hub wan1, static), 'a' = ISP-A (branch wan1, DHCP), 'b' = ISP-B (branch wan2, DHCP).
Inside the container eth0 is the uplink to the lab, eth1/eth2/eth3 are the segments."""
import hashlib
import ipaddress

from ..model import TAG_VM

SEGS = ("hub", "a", "b")
SEG_IFACE = {"hub": "eth1", "a": "eth2", "b": "eth3"}
WANEMU_PRIVILEGED = False        # Task 1 (spike on PVE01) decides whether an unprivileged CT can create ifb devices


def mac_for(dev_name, port_no):
    """Stable locally administered MAC for port<port_no> of a FortiGate: 02 + 4 bytes of sha1(name) + port."""
    h = hashlib.sha1(dev_name.encode()).digest()[:4]
    return ":".join(f"{b:02x}" for b in (0x02, *h, port_no & 0xFF))


def _port_no(name):
    return int(name[4:]) if (name or "").startswith("port") and name[4:].isdigit() else None


def _host(subnet, n):
    return str(ipaddress.ip_network(subnet, strict=False).network_address + n)


def _ip(cidr):
    return str(ipaddress.ip_interface(cidr).ip)


def reservations(t):
    """DHCP reservations: branch n (0-based, topology order) gets .res_start+n on ISP-A (wan1) and ISP-B (wan2)."""
    seg, out = t["wanemu"]["segments"], []
    for n, d in enumerate(t["edges"]):
        for s, key in (("a", "wan1_intf"), ("b", "wan2_intf")):
            port = _port_no(d.get(key))
            if port:
                out.append({"name": d["name"], "seg": s, "ip": _host(seg[s]["subnet"], seg[s]["res_start"] + n),
                            "mac": mac_for(d["name"], port)})
    return out


def links(t):
    """Every FortiGate WAN as a link: hubs (wan1 in ISP-HUB), branches (wan1 in ISP-A, wan2 in ISP-B)."""
    out = [{"id": f"{h['name']}-wan1", "name": h["name"], "seg": "hub", "iface": SEG_IFACE["hub"], "ip": _ip(h["wan1_ip"])}
           for h in t["hubs"]]
    for r in reservations(t):
        w = 1 if r["seg"] == "a" else 2
        out.append({"id": f"{r['name']}-wan{w}", "name": r["name"], "seg": r["seg"], "iface": SEG_IFACE[r["seg"]],
                    "ip": r["ip"]})
    return out


def _uplink_net(t):
    return ipaddress.ip_interface(t["wanemu"]["uplink"]["ip"]).network


def fortigates(t):
    """FortiGates in a fixed order (hubs, then branches): index n is used for forwards and LAN VLANs."""
    return [(h, True) for h in t["hubs"]] + [(e, False) for e in t["edges"]]


def lan_vlans(t):
    """{device name: (LAN1 VLAN, LAN2 VLAN)}: FortiGate n gets lan_vlan_base+1+n and lan2_vlan_base+1+n."""
    w = t["wanemu"]
    return {d["name"]: (w["lan_vlan_base"] + 1 + n, w["lan2_vlan_base"] + 1 + n) for n, (d, _) in enumerate(fortigates(t))}


def bridge_needs(t, fgts=True, clients=True):
    """{bridge: {VLAN ids}} this run puts on each Proxmox bridge: wan-emu uplink + segments and every FortiGate NIC
    (fgts), the LAN client of each spoke (clients). A bridge with an empty set is used untagged."""
    from ..provision_vms import fgt_nics
    w, need = t["wanemu"], {}
    add = lambda b, v: need.setdefault(b, set()).update([v] if v else [])
    if fgts:
        up = w["uplink"]
        add(up.get("bridge") or w["bridge"], up.get("vlan"))
        for s in SEGS:
            add(w["bridge"], w["segments"][s]["vlan"])
        for n, (d, hub) in enumerate(fortigates(t)):
            for nic in fgt_nics(d, t["pve"], w, hub, index=n):
                add(nic["bridge"], nic.get("vlan"))
    if clients:
        lv = lan_vlans(t)
        for e in t["edges"]:
            if (e.get("lans") or [{}])[0].get("ip"):
                add(w["bridge"], lv[e["name"]][0])
    return need


def _vids(text):
    out = set()
    for part in str(text).replace(",", " ").split():
        lo, _, hi = part.partition("-")
        if lo.isdigit() and (not hi or hi.isdigit()):
            out.update(range(int(lo), int(hi or lo) + 1))
    return out


def bridge_problems(need, info):
    """Messages for VLANs a bridge cannot carry. info: {iface: row of GET /nodes/{node}/network}. A VLAN-aware bridge
    only passes its bridge-vids (default 2-4094); a bridge that is not VLAN-aware gets vmbrXvY from Proxmox, which needs
    a physical port."""
    out = []
    for b, vlans in sorted(need.items()):
        row = info.get(b)
        if row is None:
            out.append(f"bridge {b} is not on this Proxmox node")
            continue
        if not vlans:
            continue
        if int(row.get("bridge_vlan_aware") or 0):
            vids = str(row.get("bridge_vids") or "2-4094")
            miss = sorted(vlans - _vids(vids))
            if miss:
                out.append(f"bridge {b} is VLAN-aware but only allows VLANs {vids}; this deployment needs "
                           f"{', '.join(map(str, miss))} (add them to bridge-vids, or choose another bridge)")
        elif not str(row.get("bridge_ports") or "").strip() or row.get("bridge_ports") == "none":
            out.append(f"bridge {b} has no physical port and is not VLAN-aware, so Proxmox cannot tag VLANs "
                       f"{', '.join(map(str, sorted(vlans)))} on it (make it VLAN-aware, or choose another bridge)")
    return out


def forwards(t):
    """Port forwards on wan-emu: FortiGate n -> its WAN1 address, HTTPS base+n -> 443, SSH base+n -> 22."""
    w = t["wanemu"]
    res = {r["name"]: r["ip"] for r in reservations(t) if r["seg"] == "a"}
    out = []
    for n, (d, hub) in enumerate(fortigates(t)):
        ip = _ip(d["wan1_ip"]) if hub else res.get(d["name"])
        if ip:
            out.append({"name": d["name"], "ip": ip, "https": w["fwd_https_base"] + n, "ssh": w["fwd_ssh_base"] + n})
    return out


def agent_topology(t):
    """What the agent in the container needs: the links, the source subnets that get NAT towards eth0, and the
    destinations that do NOT (the lab subnet: FortiManager sees the real FortiGate addresses and routes back)."""
    return {"links": links(t), "nat": [t["wanemu"]["segments"][s]["subnet"] for s in SEGS],
            "no_nat": [str(_uplink_net(t))], "forwards": forwards(t)}


def fmg_routes(t):
    """[(dst, mask, gateway)] the FortiManager needs to reach the segments via wan-emu; [] when the FortiManager is not
    in the uplink subnet (then the lab router has to route 100.127.x to the uplink address)."""
    try:
        fip = ipaddress.ip_interface(str(t["fmgr"].get("ip", "")).strip()).ip
    except ValueError:
        return []
    try:
        up = _uplink_net(t)
    except ValueError:                              # uplink not filled in (yet): e.g. a FortiManager-only run
        return []
    if fip not in up:
        return []
    gw = _ip(t["wanemu"]["uplink"]["ip"])
    out = []
    for s in SEGS:
        n = ipaddress.ip_network(t["wanemu"]["segments"][s]["subnet"], strict=False)
        out.append((str(n.network_address), str(n.netmask), gw))
    return out


def dnsmasq_conf(t):
    seg = t["wanemu"]["segments"]
    dns = ",".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x)
    lines = ["# fgt-sdwan-provision: WAN-emulator DHCP (ISP-A / ISP-B)", "port=0", "bind-interfaces",
             "dhcp-authoritative", "no-resolv"]
    for s in ("a", "b"):
        g = seg[s]
        net = ipaddress.ip_network(g["subnet"], strict=False)
        lo, hi = g["pool"]
        lines += [f"interface={SEG_IFACE[s]}",
                  f"dhcp-range=set:{s},{_host(g['subnet'], lo)},{_host(g['subnet'], hi)},{net.netmask},12h",
                  f"dhcp-option=tag:{s},option:router,{g['gw']}",
                  f"dhcp-option=tag:{s},option:dns-server,{dns}"]
    lines += [f"dhcp-host={r['mac']},{r['ip']}" for r in reservations(t)]
    return "\n".join(lines) + "\n"


def ct_params(t, vmid, ssh_pubkey):
    """Body for POST /nodes/{node}/lxc."""
    w = t["wanemu"]
    up, seg = w["uplink"], w["segments"]
    p = {"vmid": vmid, "hostname": w["hostname"], "ostemplate": w["template"], "storage": w["storage"],
         "rootfs": f"{w['storage']}:{w['disk_gb']}", "cores": w["cores"], "memory": w["memory_mb"], "swap": 0,
         "unprivileged": 0 if WANEMU_PRIVILEGED else 1, "onboot": 1, "start": 0, "tags": TAG_VM,
         # Debian 13 (systemd 257) in a CT: without nesting /tmp, /run/lock and mqueue fail to mount (spike on PVE01)
         "features": "nesting=1",
         # DNS of the topology: without it the CT inherits the PVE host's resolvers, which the uplink VLAN may not reach
         "nameserver": " ".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x),
         "ssh-public-keys": ssh_pubkey,
         "net0": f"name=eth0,bridge={up.get('bridge') or w['bridge']}" + (f",tag={up['vlan']}" if up.get("vlan") else "")
                 + f",ip={up['ip']},gw={up['gw']}"}
    for i, s in enumerate(SEGS, start=1):
        g = seg[s]
        prefix = ipaddress.ip_network(g["subnet"], strict=False).prefixlen
        p[f"net{i}"] = f"name={SEG_IFACE[s]},bridge={w['bridge']},tag={g['vlan']},ip={g['gw']}/{prefix}"
    return p


def client_params(t, spoke, vmid, password):
    """Body for POST /nodes/{node}/lxc: LAN client of one spoke on its LAN1 VLAN, .10 in the LAN (.11 when the spoke
    itself is .10), gateway = the spoke's LAN1 address."""
    w = t["wanemu"]
    lan = ipaddress.ip_interface(spoke["lans"][0]["ip"])
    host = lan.network.network_address + 10
    if host == lan.ip:
        host += 1
    return {"vmid": vmid, "hostname": f"client-{spoke['name']}", "ostemplate": w["template"], "storage": w["storage"],
            "rootfs": f"{w['storage']}:2", "cores": 1, "memory": 256, "swap": 0, "unprivileged": 1,
            "features": "nesting=1", "onboot": 1, "start": 0, "tags": TAG_VM, "password": password,
            "nameserver": " ".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x),
            "net0": f"name=eth0,bridge={w['bridge']},tag={lan_vlans(t)[spoke['name']][0]},"
                    f"ip={host}/{lan.network.prefixlen},gw={lan.ip}"}
