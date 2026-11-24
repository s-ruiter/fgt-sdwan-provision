"""WAN-emulator: everything derived from the topology (pure functions, no I/O).

Every FortiGate WAN is its own link: own VLAN and /24 (model.wan_links), routed by the wan-emu. ISPs: 'hub' = ISP-HUB
(hub wan1, static), 'a' = ISP-A (spoke wan1, DHCP), 'b' = ISP-B (spoke wan2, DHCP). Inside the container eth0 is the
uplink to the lab, eth1..ethK are the links (in model.wan_links order)."""
import hashlib
import ipaddress

from ..model import TAG_VM

SEGS = ("hub", "a", "b")
DHCP_POOL = (100, 199)           # per spoke link: .2 is the reservation of its FortiGate, the pool is for anything else
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


def links(t):
    """Every FortiGate WAN as a link (model.wan_links) with the container interface it is on: eth1, eth2, ..."""
    from ..model import wan_links
    return [dict(x, iface=f"eth{k}") for k, x in enumerate(wan_links(t), start=1)]


def reservations(t):
    """DHCP reservations: every spoke WAN gets the .2 of its own /24, on the MAC of its FortiGate port."""
    ports = {}
    for e in t["edges"]:
        ports[(e["name"], "a")], ports[(e["name"], "b")] = _port_no(e.get("wan1_intf")), _port_no(e.get("wan2_intf"))
    return [dict(x, mac=mac_for(x["name"], ports[(x["name"], x["seg"])])) for x in links(t)
            if x["seg"] != "hub" and ports.get((x["name"], x["seg"]))]


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
        for x in links(t):
            add(w["bridge"], x["vlan"])
        nh = len(t["hubs"])
        for n, (d, hub) in enumerate(fortigates(t)):
            for nic in fgt_nics(d, t["pve"], w, hub, index=n, link_no=n + 1 if hub else n - nh + 1):
                add(nic["bridge"], nic.get("vlan"))
    if clients:
        lv = lan_vlans(t)
        for e in t["edges"]:
            if (e.get("lans") or [{}])[0].get("ip"):
                add(w["bridge"], lv[e["name"]][0])
        if (t.get("trafficgen") or {}).get("enabled"):              # Traffic-Gen management on the uplink network
            up = w["uplink"]
            add(up.get("bridge") or w["bridge"], up.get("vlan"))
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


def uplink_problems(t, info):
    """The uplink is untagged on a bridge where the PVE host has an address in another network: then the uplink VLAN
    is almost certainly missing (wan-emu would land in the host's network and never be reachable)."""
    w = t["wanemu"]
    up, b = w["uplink"], w["uplink"].get("bridge") or w["bridge"]
    host = str((info.get(b) or {}).get("cidr") or "")
    if up.get("vlan") or not host:
        return []
    try:
        hnet, unet = ipaddress.ip_interface(host).network, ipaddress.ip_interface(str(up.get("ip", ""))).network
    except ValueError:
        return []
    if unet.overlaps(hnet):
        return []
    return [f"WAN emulation uplink is untagged on {b}, but untagged {b} is the network {hnet} (the Proxmox host's "
            f"address) and the uplink IP is in {unet}: fill in the Uplink VLAN of {unet}"]


def forwards(t):
    """Port forwards on wan-emu: FortiGate n -> its WAN1 address, HTTPS base+n -> 443, SSH base+n -> 22."""
    w = t["wanemu"]
    wan1 = {x["name"]: x["ip"] for x in links(t) if x["id"].endswith("-wan1")}
    out = []
    for n, (d, hub) in enumerate(fortigates(t)):
        ip = wan1.get(d["name"])
        if ip:
            out.append({"name": d["name"], "ip": ip, "https": w["fwd_https_base"] + n, "ssh": w["fwd_ssh_base"] + n})
    return out


def agent_topology(t):
    """What the agent in the container needs: the links, the source subnets that get NAT towards eth0, and the
    destinations that do NOT (the lab subnet: FortiManager sees the real FortiGate addresses and routes back)."""
    return {"links": links(t), "nat": [t["wanemu"]["segments"][s]["block"] for s in SEGS],
            "no_nat": [str(_uplink_net(t))], "forwards": forwards(t)}


def fmg_routes(t):
    """[(dst, mask, gateway)] the FortiManager needs to reach the segments via wan-emu; [] when the FortiManager is not
    in the uplink subnet (then the lab router has to route the ISP blocks to the uplink address)."""
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
    for s in SEGS:                                   # one route per ISP block (all its link /24s)
        n = ipaddress.ip_network(t["wanemu"]["segments"][s]["block"], strict=False)
        out.append((str(n.network_address), str(n.netmask), gw))
    return out


def dnsmasq_conf(t):
    """DHCP on every spoke link (hub links are static): a small pool in its /24 and the reservation (.2) of its
    FortiGate; router = the wan-emu (.1) of that link."""
    dns = ",".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x)
    lines = ["# fgt-sdwan-provision: WAN-emulator DHCP (one /24 per spoke WAN)", "port=0", "bind-interfaces",
             "dhcp-authoritative", "no-resolv"]
    for x in links(t):
        if x["seg"] == "hub":
            continue
        net, tag = ipaddress.ip_network(x["subnet"]), x["iface"]
        lines += [f"interface={tag}",
                  f"dhcp-range=set:{tag},{_host(x['subnet'], DHCP_POOL[0])},{_host(x['subnet'], DHCP_POOL[1])},{net.netmask},12h",
                  f"dhcp-option=tag:{tag},option:router,{x['gw']}",
                  f"dhcp-option=tag:{tag},option:dns-server,{dns}"]
    lines += [f"dhcp-host={r['mac']},{r['ip']}" for r in reservations(t)]
    return "\n".join(lines) + "\n"


def ct_params(t, vmid, ssh_pubkey):
    """Body for POST /nodes/{node}/lxc."""
    w = t["wanemu"]
    up = w["uplink"]
    from ..model import pve_name
    p = {"vmid": vmid, "hostname": pve_name(t, w["hostname"]), "ostemplate": w["template"], "storage": w["storage"],
         "rootfs": f"{w['storage']}:{w['disk_gb']}", "cores": w["cores"], "memory": w["memory_mb"], "swap": 0,
         "unprivileged": 0 if WANEMU_PRIVILEGED else 1, "onboot": 1, "start": 0, "tags": TAG_VM,
         # Debian 13 (systemd 257) in a CT: without nesting /tmp, /run/lock and mqueue fail to mount (spike on PVE01)
         "features": "nesting=1",
         # DNS of the topology: without it the CT inherits the PVE host's resolvers, which the uplink VLAN may not reach
         "nameserver": " ".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x),
         "ssh-public-keys": ssh_pubkey,
         "net0": f"name=eth0,bridge={up.get('bridge') or w['bridge']}" + (f",tag={up['vlan']}" if up.get("vlan") else "")
                 + f",ip={up['ip']},gw={up['gw']}"}
    for k, x in enumerate(links(t), start=1):          # one NIC per WAN link (Proxmox: net0..net31)
        p[f"net{k}"] = (f"name={x['iface']},bridge={w['bridge']},tag={x['vlan']},ip={x['gw']}/24,firewall=0,"
                        f"hwaddr={mac_for(w['hostname'] + '-link', k).upper()}")   # stable across re-deploys
    return p


def client_params(t, spoke, vmid, password):
    """Body for POST /nodes/{node}/lxc: LAN client of one spoke on its LAN1 VLAN, .10 in the LAN (.11 when the spoke
    itself is .10), gateway = the spoke's LAN1 address."""
    w = t["wanemu"]
    lan = ipaddress.ip_interface(spoke["lans"][0]["ip"])
    host = lan.network.network_address + 10
    if host == lan.ip:
        host += 1
    from ..model import pve_name
    return {"vmid": vmid, "hostname": pve_name(t, f"client-{spoke['name']}"), "ostemplate": w["template"], "storage": w["storage"],
            "rootfs": f"{w['storage']}:2", "cores": 1, "memory": 256, "swap": 0, "unprivileged": 1,
            "features": "nesting=1", "onboot": 1, "start": 0, "tags": TAG_VM, "password": password,
            "nameserver": " ".join(x for x in (t.get("dns_primary"), t.get("dns_secondary")) if x),
            "net0": f"name=eth0,bridge={w['bridge']},tag={lan_vlans(t)[spoke['name']][0]},"
                    f"ip={host}/{lan.network.prefixlen},gw={lan.ip}"}
