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


def agent_topology(t):
    """What the agent in the container needs: the links, the source subnets that get NAT towards eth0, and the
    destinations that do NOT (the lab subnet: FortiManager sees the real FortiGate addresses and routes back)."""
    return {"links": links(t), "nat": [t["wanemu"]["segments"][s]["subnet"] for s in SEGS],
            "no_nat": [str(_uplink_net(t))]}


def fmg_routes(t):
    """[(dst, mask, gateway)] the FortiManager needs to reach the segments via wan-emu; [] when the FortiManager is not
    in the uplink subnet (then the lab router has to route 100.127.x to the uplink address)."""
    try:
        fip = ipaddress.ip_interface(str(t["fmgr"].get("ip", "")).strip()).ip
    except ValueError:
        return []
    if fip not in _uplink_net(t):
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
         "net0": f"name=eth0,bridge={w['bridge']},tag={up['vlan']},ip={up['ip']},gw={up['gw']}"}
    for i, s in enumerate(SEGS, start=1):
        g = seg[s]
        prefix = ipaddress.ip_network(g["subnet"], strict=False).prefixlen
        p[f"net{i}"] = f"name={SEG_IFACE[s]},bridge={w['bridge']},tag={g['vlan']},ip={g['gw']}/{prefix}"
    return p
