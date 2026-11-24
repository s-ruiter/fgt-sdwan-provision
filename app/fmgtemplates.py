"""Native FortiManager provisioning objects from the Fortinet Jinja Orchestrator.

No Jinja ends up in FortiManager. The Orchestrator is rendered *symbolically*: every per-device value (hostname,
loopback, port names, LAN addresses ...) is replaced by a FortiManager metadata variable `$(name)`, and the
`ipaddr()` filter returns derived variables (`$(lan_ip_netmask)`, `$(lan_ip_n4_address)` ...) whose per-device
values are computed here. Result per role (HUB1, HUB2, SPOKES ...):

  * CLI templates for underlay / overlay / routing / inter-VRF  (core Orchestrator files 01-07)
  * a CLI template group, a system template (DNS), a native SD-WAN template (wanprof), a static-route template,
    a policy package, normalized interfaces, address/ip-pool objects, a device group and a device blueprint
  * metadata-variable values per device

SD-WAN and firewall come from Orchestrator files `optional/11` and `optional/12`, converted from CLI to the
FortiManager JSON structure (`cli_to_json`).
"""
import ipaddress
import json
import os
import re
from pathlib import Path

import jinja2

from . import model
from .ipfilter import ipaddr as real_ipaddr

FLAVOR = "dynamic-bgp-on-lo"
VAR_RE = re.compile(r"\$\(([A-Za-z0-9_]+)\)")
PE_VRF = 1


# --------------------------------------------------------------------------- symbolic rendering
class Registry:
    def __init__(self):
        self.kind, self.der, self.base = {}, {}, {}

    def var(self, name, kind, value):
        self.kind[name], self.base[name] = kind, value
        return f"$({name})"

    def value(self, name):
        if name in self.base:
            return self.base[name]
        b, q = self.der[name]
        return real_ipaddr(self.value(b), q)


def _is_var(v):
    return isinstance(v, str) and v.startswith("$(") and v.endswith(")")


def make_ipaddr(reg):
    def ipaddr(value, query=None):
        if not _is_var(value):
            return real_ipaddr(value, query)
        n = value[2:-1]
        if query is None or query == "":
            return value
        if isinstance(query, bool):
            return False
        if isinstance(query, int):
            nn = f"{n}_n{query}".replace("-", "m")
            reg.kind[nn], reg.der[nn] = "cidr", (n, query)
            return f"$({nn})"
        if query == "address" and reg.kind.get(n) == "ip":
            return value
        nn = f"{n}_{query}"
        reg.kind[nn], reg.der[nn] = "ip", (n, query)
        return f"$({nn})"
    return ipaddr


def _clean(text):
    return "\n".join(l.rstrip() for l in text.split("\n") if l.strip())


def symbolize(meta, reg, is_hub):
    """Replace per-device values in an inventory entry by metadata variables."""
    m = dict(meta)
    if not is_hub:                               # hub templates are per hub: hostname stays literal
        m["hostname"] = reg.var("hostname", "name", meta["hostname"])
    m["loopback"] = reg.var("loopback", "ip", meta["loopback"])
    for k in list(m):
        if k.endswith("_intf"):
            m[k] = reg.var(k, "name", meta[k])
        elif re.fullmatch(r"lan\d*_ip", k):
            m[k] = reg.var(k, "cidr", meta[k])
    if meta.get("wan1_addr", "dhcp") != "dhcp":
        m["wan1_addr"] = reg.var("wan1_ip", "cidr", meta["wan1_addr"])
    return m


def render_device(orch_dir, project, meta, is_hub):
    """-> dict(core={template-file: text}, sdwan=text, firewall=text, reg=Registry)"""
    reg = Registry()
    flavor = Path(orch_dir) / FLAVOR
    env = jinja2.Environment(loader=jinja2.ChoiceLoader([jinja2.DictLoader({"00-Project": project}),
                                                         jinja2.FileSystemLoader(str(flavor))]),
                             undefined=jinja2.StrictUndefined)
    env.filters["ipaddr"] = make_ipaddr(reg)
    group = "Hub" if is_hub else "Edge"
    m = symbolize(meta, reg, is_hub)

    def r(name):
        return _clean(env.get_template(name).render(m))
    core = {n: r(n) for n in sorted(f for f in os.listdir(flavor) if group in f and f.endswith(".j2"))}
    opt = flavor / "optional"
    sd = next(f for f in os.listdir(opt) if group in f and "SDWAN" in f)
    fw = next(f for f in os.listdir(opt) if group in f and "Firewall" in f)
    return {"core": core, "sdwan": r("optional/" + sd), "firewall": r("optional/" + fw), "reg": reg}


# --------------------------------------------------------------------------- CLI -> FortiManager JSON
LIST_KEYS = {"members", "priority-members", "member", "srcintf", "dstintf", "srcaddr", "dstaddr", "service",
             "application-list", "ssl-ssh-profile", "advpn-health-check", "sdwan-zone", "poolname", "internet-service-app-ctrl",
             "dst", "src"}
SCALAR_KEYS = {"dst", "src"}            # service dst/src are address names (lists in FMG) but static-route dst is a subnet
TABLE_KEY = {"members": "seq-num", "health-check": "name", "neighbor": "ip", "service": "id", "zone": "name",
             "policy": "policyid", "static": "seq-num", "address": "name", "ippool": "name"}
KNOWN_TOP = {"system sdwan", "router static", "firewall address", "firewall ippool", "firewall policy"}


def _tok(line):
    return re.findall(r'"[^"]*"|\S+', line)


def _unq(s):
    return s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def _parse(lines, i=0):
    items = []
    while i < len(lines):
        t = _tok(lines[i].strip())
        if not t or t[0].startswith("#"):
            i += 1
            continue
        w = t[0]
        if w == "config":
            sub, i = _parse(lines, i + 1)
            items.append(("config", " ".join(t[1:]), sub))
        elif w == "edit":
            sub, i = _parse(lines, i + 1)
            items.append(("edit", _unq(t[1]), sub))
        elif w in ("next", "end"):
            return items, i + 1
        else:
            if w == "set":
                items.append(("set", t[1], [_unq(x) for x in t[2:]]))
            i += 1
    return items, i


def _conv(key, toks, static_dst=False):
    if key in LIST_KEYS and not (static_dst and key == "dst"):
        return [str(x) for x in toks]
    if len(toks) == 1:
        return int(toks[0]) if re.fullmatch(r"\d+", toks[0]) and key not in ("name", "server", "interface") else toks[0]
    return [str(x) for x in toks]


def _body(items, parent_tbl, static_dst=False):
    d = {}
    for kind, key, val in items:
        if kind == "set":
            d[key] = _conv(key, val, static_dst)
        elif kind == "config":
            if any(k == "edit" for k, _, _ in val):
                kf = TABLE_KEY.get(key, "name")
                if key == "sla":
                    kf = "id" if parent_tbl == "health-check" else "health-check"
                rows = d.setdefault(key, [])
                for k2, name, sub in val:
                    if k2 != "edit":
                        continue
                    row = _body(sub, key, static_dst)
                    row[kf] = int(name) if re.fullmatch(r"\d+", name) and kf in ("seq-num", "id", "policyid") else name
                    ex = next((r for r in rows if r.get(kf) == row[kf]), None)
                    if ex is not None:
                        ex.update(row)
                    else:
                        rows.append(row)
            else:
                d.setdefault(key, {}).update(_body(val, key, static_dst))
    return d


def cli_to_json(text):
    """-> ({top-level path: body}, [unsupported top-level blocks]). Tables become lists of dicts."""
    items, _ = _parse(text.split("\n"))
    out, unsupported = {}, []
    for kind, path, sub in items:
        if kind != "config":
            continue
        if path not in KNOWN_TOP:
            unsupported.append(path)
            continue
        if any(k == "edit" for k, _, _ in sub):
            rows = []
            tmp = _body([("config", path.split()[-1], sub)], None, static_dst=(path == "router static"))
            rows = tmp[path.split()[-1]]
            out[path] = rows
        else:
            out[path] = _body(sub, path.split()[-1])
    return out, unsupported


def _subnet(cidr):
    import ipaddress
    n = ipaddress.ip_network(cidr, strict=False)
    return [str(n.network_address), str(n.netmask)]


# --------------------------------------------------------------------------- plan
def _short(fname):
    m = re.match(r"(\d+)-(?:Edge|Hub)-(.+)\.j2", fname)
    return f"{m.group(1)}-{m.group(2)}" if m else fname


def mgmt_cli(dev, pe=PE_VRF):
    """ZTP/management fragment: the FMG tunnel follows the VRF move of wan1.
    No static route for a static (hub) wan1: wan1 is an SD-WAN member, so a route on it next to the SD-WAN default route
    (seq 100, zone underlay) is refused ("Duplicate routes (1 and 100) on SD-WAN and/or non-SD-WAN interfaces", FMG
    8.0.2 install). The gateway goes on the SD-WAN member instead (see _hub_gateway)."""
    lines = []
    if pe:      # the FortiGate's own traffic to FMG and DNS leaves in the PE-VRF, where the WANs are
        lines += ["config system central-management", f"  set vrf-select {pe}", "end",
                  "config system dns", f"  set vrf-select {pe}", "end"]
    return "\n".join(lines)


def build_plan(topo, orch_dir):
    """Return a plan dict (see module docstring). Raises ValueError on inconsistent input."""
    project, inv = model.build(topo)
    devs = {d["name"]: (d, True) for d in topo["hubs"]}
    devs.update({d["name"]: (d, False) for d in topo["edges"]})
    per = {}
    for name, (d, is_hub) in devs.items():
        meta = (inv["Hub"] if is_hub else inv["Edge"])[name]
        s = render_device(orch_dir, project, meta, is_hub)
        _leak_return_routes(s, meta, topo)
        if not is_hub:
            rename_edge_tunnels(s)
        mg = mgmt_cli(d, model.pe_vrf(topo))
        reg = s["reg"]
        values = {v: reg.value(v) for v in set(VAR_RE.findall("\n".join(list(s["core"].values()) + [s["sdwan"], s["firewall"]])))}
        values["wan1_intf"] = d["wan1_intf"]
        values["hostname"] = d["name"]           # hub templates use the literal name, but the variable shows it per device too
        s.update(is_hub=is_hub, dev=d, mgmt=mg, values=values)
        per[name] = s

    # roles: one per hub; branches share a role when everything they render is identical
    roles, edge_sigs = [], {}
    for i, h in enumerate(topo["hubs"], start=1):
        roles.append({"name": f"HUB{i}", "kind": "hub", "devices": [h["name"]], "proto": h["name"]})
    for e in topo["edges"]:
        s = per[e["name"]]
        sig = json.dumps([s["core"], s["sdwan"], s["firewall"], s["mgmt"]], sort_keys=True)
        edge_sigs.setdefault(sig, []).append(e["name"])
    groups = list(edge_sigs.values())
    for k, names in enumerate(groups, start=1):
        roles.append({"name": "SPOKES" if len(groups) == 1 else f"SPOKES-{k}", "kind": "edge", "devices": names, "proto": names[0]})

    warnings, variables = [], {}
    for r in roles:
        s = per[r["proto"]]
        role = r["name"]
        r["cli"] = []
        mgmt_vars = sorted(set(VAR_RE.findall(s["mgmt"])))
        if s["mgmt"].strip():
            r["cli"].append((f"{role}_00-Mgmt", s["mgmt"], mgmt_vars))
        for fname, text in s["core"].items():
            if text.strip():
                r["cli"].append((f"{role}_{_short(fname)}", text, sorted(set(VAR_RE.findall(text)))))
        sd, un1 = cli_to_json(s["sdwan"])
        fw, un2 = cli_to_json(s["firewall"])
        for u in un1 + un2:
            warnings.append(f"{role}: Orchestrator block '{u}' could not be converted to a native object and is missing")
        r["sdwan"] = sd.get("system sdwan", {})
        lit = _literal_intfs(r, per)
        r["sdwan"] = _subst(r["sdwan"], lit)
        if r["kind"] == "hub":
            _hub_gateway(r["sdwan"], per[r["proto"]]["dev"])
        if model.pe_vrf(topo):      # health-check probes in the PE-VRF (in VRF 0 they had no route / failed RPF)
            for hc in r["sdwan"].get("health-check", []):
                if hc.get("detect-mode") != "remote":
                    hc["vrf"] = model.pe_vrf(topo)
        if r["kind"] == "edge":
            add_demo_rules(r["sdwan"])
        r["static"] = sd.get("router static", [])
        for row in r["static"]:
            row.setdefault("dst", ["0.0.0.0", "0.0.0.0"])
            row.setdefault("status", "enable")
            row["_seq-num"] = row.get("seq-num")
        _fmg_host_route(r, topo, warnings)
        addr = {}
        for a in sd.get("firewall address", []) + fw.get("firewall address", []):
            addr[a["name"]] = {"name": a["name"], "type": 0, "associated-interface": "any", "color": 22,
                               "subnet": _subnet(a["subnet"]) if isinstance(a.get("subnet"), str) else a.get("subnet")}
        r["addresses"] = list(addr.values())
        r["policies"] = fw.get("firewall policy", [])
        if r["kind"] == "hub":
            _hub_leak_dia(r, s)
        for p in r["policies"]:
            if p.get("name") == "Internet (to PE)":            # no NAT into the PE-VRF, see _leak_return_routes
                p["nat"] = "disable"
                for k in ("ippool", "poolname"):
                    p.pop(k, None)
        r["ippools"] = fw.get("firewall ippool", [])
        zones = {z["name"] for z in r["sdwan"].get("zone", [])}
        names = set()
        for p in r["policies"]:
            for k in ("srcintf", "dstintf"):
                v = p.get(k, [])
                names |= set(v if isinstance(v, list) else [v])
        r["dyn_ifaces"] = sorted(n for n in names if n not in zones and n != "any")
        for dn in r["devices"]:
            for v, val in per[dn]["values"].items():
                variables.setdefault(v, {})[dn] = val
        for dn in r["devices"]:                       # variables of the shared role must exist for every device of it
            miss = [v for _, _, vs in r["cli"] for v in vs if v not in per[dn]["values"]]
            if miss:
                raise ValueError(f"{dn}: variables missing for role {role}: {sorted(set(miss))}")
        r["ippool_map"] = {dn: _ippool_values(r["ippools"], per[dn]["reg"]) for dn in r["devices"]}
    plan = {"roles": roles, "variables": variables, "warnings": warnings, "per": per,
            "dns": [topo["dns_primary"], topo.get("dns_secondary", "")], "adom_meta": {}}
    check_dyn_ifaces(plan)
    return plan


def check_dyn_ifaces(plan):
    """Every interface/zone a policy uses must exist on the device: an SD-WAN zone or something a CLI template creates
    (edit "<name>"). Otherwise FortiManager aborts the policy install ('Dynamic interface ... mapping undefined')."""
    for r in plan["roles"]:
        cli = "\n".join(x for _, x, _ in r["cli"])
        have = {z["name"] for z in r["sdwan"].get("zone", [])} | set(re.findall(r'edit "([^"]+)"', cli))
        missing = [n for n in r["dyn_ifaces"] if n not in have]
        if missing:
            raise ValueError(f"{r['name']}: firewall policies use {', '.join(missing)}, which no template creates on "
                             "the device: FortiManager would abort the policy install")


# Demo SD-WAN rules for the Traffic-Gen apps (spokes with two WANs: member 1 = wan1, member 2 = wan2). The apps are
# matched with ISDB internet services; the keywords are resolved against the FortiManager's list at deployment
# (fmgapply.resolve_isdb), because the exact names differ per FortiOS version.
DEMO_RULES = [
    {"id": 2, "name": "O365", "mode": "priority", "priority-members": ["2", "1"], "health-check": "Internet",
     "keywords": ["office365", "office 365", "outlook", "sharepoint", "onedrive"]},
    {"id": 3, "name": "Collab", "mode": "sla", "priority-members": ["2", "1"], "sla": [{"health-check": "Internet", "id": 1}],
     "keywords": ["teams", "zoom", "webex", "google meet", "meet", "slack"]},
    {"id": 4, "name": "Streaming-Social", "mode": "priority", "priority-members": ["1"], "health-check": "Internet",
     "keywords": ["netflix", "youtube", "spotify", "disney", "prime video", "primevideo", "twitch", "facebook",
                  "instagram", "whatsapp", "linkedin", "twitter", "tiktok", "reddit"]},
    {"id": 5, "name": "Updates-Downloads", "mode": "load-balance", "priority-members": ["1", "2"],
     "keywords": ["windows update", "windowsupdate", "apple update", "software update", "speedtest", "ookla"]},
    {"id": 6, "name": "SaaS", "mode": "priority", "priority-members": ["1", "2"], "health-check": "Internet",
     "link-cost-factor": "latency",
     "keywords": ["salesforce", "google workspace", "gsuite", "dropbox", "box", "github", "aws", "adobe", "servicenow"]},
]
INTERNET_SLA = {"id": 1, "latency-threshold": 150, "jitter-threshold": 30, "packetloss-threshold": 2}


def add_demo_rules(sdwan):
    """Spoke SD-WAN with two underlay members: demo rules between Corporate (1) and the rest; SLA on 'Internet'."""
    under = [m for m in sdwan.get("members", []) if m.get("zone") == "underlay"]
    hcs = {h["name"]: h for h in sdwan.get("health-check", [])}
    if len(under) != 2 or "Internet" not in hcs:
        return
    rows = [{k: v for k, v in r.items() if k != "keywords"} | {"_isdb_keywords": list(r["keywords"])} for r in DEMO_RULES]
    sv = sdwan.setdefault("service", [])
    pos = next((i + 1 for i, x in enumerate(sv) if x.get("id") == 1), 0)
    sv[pos:pos] = [dict(x) for x in rows]
    hcs["Internet"]["sla"] = [dict(INTERNET_SLA)]


LEAK1_RE = re.compile(r'edit "vrf(\d+)_leak1"(.*?)\n\s*next', re.S)


def _leak_return_routes(s, meta, topo):
    """Multi-VRF: traffic from a LAN VRF to the PE-VRF (DIA, RIA) goes without NAT. With NAT (the Orchestrator's
    'Internet (to PE)') FortiOS treats the address it NATs to (leak IP or ip-pool) as local: the reply in the PE-VRF is
    delivered to the FortiGate itself and never goes back over the leak link (flow trace: 'find a route:
    flag=80000000 gw-0.0.0.0 via root'). So the PE-VRF needs a route back to every LAN with DIA: LAN subnet ->
    vrfN_leak0, gateway = the vrfN_leak1 address. The LAN subnet is a per-device variable (lanX_ip_subnet)."""
    if not model._multi(topo):
        return
    lan_of = {v["id"]: model._lan_var(k, "ip") for k, v in enumerate(topo["region"]["vrfs"])}
    reg = s["reg"]
    for fname, text in s["core"].items():
        rows = []
        for vrf, body in LEAK1_RE.findall(text):
            ip = re.search(r"set ip (\S+)/31", body)
            lv = lan_of.get(int(vrf))
            if not ip or not lv or not model._cidr(meta.get(lv) or ""):
                continue
            net = ipaddress.ip_interface(meta[lv]).network
            var = reg.var(f"{lv}_subnet", "ip", f"{net.network_address} {net.netmask}")
            rows += [f"    edit {4100 + int(vrf)}", f"      set dst {var}", f"      set gateway {ip.group(1)}",
                     f'      set device "vrf{vrf}_leak0"', "    next"]
        if rows:
            s["core"][fname] = text + "\n" + "\n".join(["config router static"] + rows + ["end"])


LEAK_ZONE_RE = re.compile(r'edit "pevrf_leak_zone"\s*\n\s*set interface \S')


def _hub_leak_dia(r, s):
    """Multi-VRF hub: the LAN sits in VRF 11/12 and its default route goes over the leak link (vrfN_leak1) to the
    PE-VRF, where the underlay is. The Orchestrator hub firewall only has 'Internet (DIA)' lan_zone -> underlay, which
    never matches there. Make it like the spokes: 'Internet (to PE)' lan_zone -> vrfs_leak_zone (NAT), and DIA from
    pevrf_leak_zone -> underlay. Only when the hub actually has leak links (a LAN VRF with DIA)."""
    if not any(LEAK_ZONE_RE.search(t) for t in s["core"].values()):
        return
    pols = r["policies"]
    dia = next((p for p in pols if p.get("name") == "Internet (DIA)"), None)
    if dia is None or any(p.get("name") == "Internet (to PE)" for p in pols):
        return
    to_pe = {k: v for k, v in dia.items() if k != "policyid"}
    to_pe.update(name="Internet (to PE)", srcintf=["lan_zone"], dstintf=["vrfs_leak_zone"], nat="disable",
                 policyid=max(int(p.get("policyid", 0)) for p in pols) + 10)
    dia["srcintf"] = ["pevrf_leak_zone"]
    pols.insert(pols.index(dia), to_pe)


def _literal_intfs(role, per):
    """Native SD-WAN members cannot hold a metadata variable as interface: use the port name, which must be the
    same on every device of the role (otherwise the user must align the ports or split the role)."""
    lit = {}
    for var in ("wan1_intf", "wan2_intf"):
        vals = {per[d]["values"].get(var) for d in role["devices"]}
        if len(vals) > 1:
            raise ValueError(f"Role {role['name']} ({', '.join(role['devices'])}): {var} differs per device ({sorted(map(str, vals))}); "
                             "the native SD-WAN template needs the same port name within a role")
        if vals != {None}:
            lit[var] = vals.pop()
    return lit


_P1 = re.compile(r"config vpn ipsec phase1-interface\n((?:.|\n)*?)\nend")
_P1_EDIT = re.compile(r'edit "([^"]+)"((?:.|\n)*?)\n  next')


def rename_edge_tunnels(s):
    """Branch tunnels get '<hub>-WAN<n>' after the branch wan they leave from (H1-WAN1 on wan1, H1-WAN2 on wan2) instead
    of the Orchestrator's '<hub>_<overlay type>[_index]' (H1_WAN1, H1_WAN1_2: every branch wan uses overlay type WAN1).
    Renames every reference in the rendered config (phase1/phase2, SD-WAN, routing, firewall). -> {old: new}"""
    names = {}
    for blk in _P1.findall("\n".join(s["core"].values())):
        for name, body in _P1_EDIT.findall(blk):
            m = re.search(r'set interface "\$\(wan(\d+)_intf\)"', body)
            hub = re.match(r"(.+?)_WAN\d+(?:_\d+)?$", name)
            if m and hub:
                names[name] = f"{hub.group(1)}-WAN{m.group(1)}"
    if len(set(names.values())) != len(names):
        raise ValueError(f"tunnel names not unique after renaming: {names}")
    if names:
        pat = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")(?![\w-])")
        sub = lambda text: pat.sub(lambda m: names[m.group(1)], text)
        s["core"] = {k: sub(v) for k, v in s["core"].items()}
        s["sdwan"], s["firewall"] = sub(s["sdwan"]), sub(s["firewall"])
    return names


FMG_ROUTE_SEQ = 110        # host route to the FortiManager (the Orchestrator's own routes use 100-102)


def _fmg_host_route(r, topo, warnings):
    """The Orchestrator blackholes the LAN/loopback summaries (e.g. 10.0.0.0/8 'Avoid potential leak of corporate traffic
    to underlay'). A FortiManager inside such a range is then unreachable once the template is installed: the FGFM tunnel
    drops ('tunnel down'). A /32 via the underlay zone is more specific and keeps it reachable."""
    ip = str(topo.get("fmgr", {}).get("ip", "")).split("/")[0].strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        return
    if not any(z.get("name") == "underlay" for z in r["sdwan"].get("zone", [])):
        warnings.append(f"{r['name']}: no SD-WAN zone 'underlay', no host route to FortiManager {ip}")
        return
    r["static"].append({"seq-num": FMG_ROUTE_SEQ, "_seq-num": FMG_ROUTE_SEQ, "dst": [ip, "255.255.255.255"],
                        "sdwan-zone": ["underlay"], "status": "enable", "comment": "FortiManager (fgt-sdwan-provision)"})


def _hub_gateway(sdwan, dev):
    """Static hub wan1: the default gateway belongs on the SD-WAN member (the SD-WAN default route, seq 100, uses it).
    One role per hub, so the literal gateway of that hub can go into its SD-WAN template."""
    gw = (dev.get("wan1_gw") or "").strip()
    for m in sdwan.get("members", []):
        if m.get("interface") == dev.get("wan1_intf") and gw:
            m["gateway"] = gw


def _subst(obj, lit):
    if isinstance(obj, str):
        return VAR_RE.sub(lambda m: str(lit[m.group(1)]) if m.group(1) in lit else m.group(0), obj)
    if isinstance(obj, list):
        return [_subst(x, lit) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, lit) for k, v in obj.items()}
    return obj


def _ippool_values(pools, reg):
    out = []
    for p in pools:
        row = {}
        for k in ("startip", "endip"):
            v = p.get(k, "")
            row[k] = VAR_RE.sub(lambda m: str(reg.value(m.group(1))), v)
        out.append((p["name"], row))
    return out


def preview(plan):
    """Text for the UI: every CLI template, native SD-WAN/static/policy JSON per role, per-device variable values."""
    out = {}
    for r in plan["roles"]:
        for name, text, vs in r["cli"]:
            out[f"CLI {name}"] = text + "\n"
        out[f"SD-WAN {r['name']}"] = json.dumps({"static-route": r["static"], "system sdwan": r["sdwan"]}, indent=1)
        out[f"Policy {r['name']}"] = json.dumps({"addresses": r["addresses"], "interfaces": r["dyn_ifaces"], "policies": r["policies"]}, indent=1)
    devs = sorted({d for v in plan["variables"].values() for d in v})
    out["Variabelen"] = json.dumps({d: {v: vals[d] for v, vals in sorted(plan["variables"].items()) if d in vals} for d in devs}, indent=1)
    return out
