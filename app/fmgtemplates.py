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
    if meta.get("isp1_addr", "dhcp") != "dhcp":
        m["isp1_addr"] = reg.var("wan1_ip", "cidr", meta["isp1_addr"])
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
    """ZTP/management fragment: default route for a static wan1 + FMG tunnel follows the VRF move of wan1."""
    lines = []
    if dev.get("wan1_mode") == "static":
        lines += ["config router static", "  edit 1", "    set gateway $(wan1_gw)", '    set device "$(isp1_intf)"',
                  *([f"    set vrf {pe}"] if pe else []), "  next", "end"]
    if pe:
        lines += ["config system central-management", f"  set vrf-select {pe}", "end"]
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
        mg = mgmt_cli(d, model.pe_vrf(topo))
        reg = s["reg"]
        values = {v: reg.value(v) for v in set(VAR_RE.findall("\n".join(list(s["core"].values()) + [s["sdwan"], s["firewall"]])))}
        if d.get("wan1_mode") == "static":
            values["wan1_gw"] = d["wan1_gw"].strip()
        values["isp1_intf"] = d["isp1_intf"]
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
            warnings.append(f"{role}: Orchestrator-blok '{u}' kon niet naar een native object worden omgezet en ontbreekt")
        r["sdwan"] = sd.get("system sdwan", {})
        lit = _literal_intfs(r, per)
        r["sdwan"] = _subst(r["sdwan"], lit)
        r["static"] = sd.get("router static", [])
        for row in r["static"]:
            row.setdefault("dst", ["0.0.0.0", "0.0.0.0"])
            row.setdefault("status", "enable")
            row["_seq-num"] = row.get("seq-num")
        addr = {}
        for a in sd.get("firewall address", []) + fw.get("firewall address", []):
            addr[a["name"]] = {"name": a["name"], "type": 0, "associated-interface": "any", "color": 22,
                               "subnet": _subnet(a["subnet"]) if isinstance(a.get("subnet"), str) else a.get("subnet")}
        r["addresses"] = list(addr.values())
        r["policies"] = fw.get("firewall policy", [])
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
                raise ValueError(f"{dn}: variabelen ontbreken voor rol {role}: {sorted(set(miss))}")
        r["ippool_map"] = {dn: _ippool_values(r["ippools"], per[dn]["reg"]) for dn in r["devices"]}
    return {"roles": roles, "variables": variables, "warnings": warnings, "per": per,
            "dns": [topo["dns_primary"], topo.get("dns_secondary", "")], "adom_meta": {}}


def _literal_intfs(role, per):
    """Native SD-WAN members cannot hold a metadata variable as interface: use the port name, which must be the
    same on every device of the role (otherwise the user must align the ports or split the role)."""
    lit = {}
    for var in ("isp1_intf", "isp2_intf", "mpls_intf"):
        vals = {per[d]["values"].get(var) for d in role["devices"]}
        if len(vals) > 1:
            raise ValueError(f"Rol {role['name']} ({', '.join(role['devices'])}): {var} verschilt per device ({sorted(map(str, vals))}); "
                             "de native SD-WAN-template vereist dezelfde poortnaam binnen een rol")
        if vals != {None}:
            lit[var] = vals.pop()
    return lit


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
