"""CSV import/export of the topology (everything that is not a secret).

One file, three sections, each introduced by a `[section]` line followed by its own header row:

    [algemeen]   key;value                (global settings, region, FortiManager)
    [vrf]        id;lan_summary;dia       (only used with multi-VRF)
    [devices]    type;name;serial;...     (type = hub | branch; lan1_intf/lan1_ip, lan2_intf/lan2_ip ...)

Lines starting with `#` and empty lines are ignored. The delimiter (; or ,) is detected, so a file saved by
Excel (NL: `;`) or by another tool imports fine. Tokens, passwords and Proxmox secrets are never in the file.
"""
import csv
import io
import re

from . import model

# key (dotted path into the topology) -> (label, kind)
SETTINGS = [
    ("adom", "ADOM-naam", "str"),
    ("os_ver", "ADOM-versie (7.6 of 8.0)", "str"),
    ("platform", "Platform (blueprint/model device)", "str"),
    ("dns_primary", "DNS primair", "str"),
    ("dns_secondary", "DNS secundair (optioneel)", "str"),
    ("wan1_access", "allowaccess op wan1", "str"),
    ("psk", "IPsec PSK (leeg = template-default; staat leesbaar in dit bestand!)", "str"),
    ("hub2hub", "Hub-naar-hub tunnels (ja/nee)", "bool"),
    ("lo_summary", "Loopback-summary (globaal)", "str"),
    ("fmgr.name", "FortiManager hostname", "str"),
    ("fmgr.ip", "FortiManager IP/prefix, bv. 10.0.0.5/24", "str"),
    ("fmgr.gateway", "FortiManager gateway", "str"),
    ("fmgr.port", "FortiManager poort (ZTP)", "str"),
    ("fmgr.initial_ip", "FortiManager initieel IP (alleen methode ssh)", "str"),
    ("region.name", "Regio", "str"),
    ("region.bgp_as", "BGP AS", "int"),
    ("region.lo_summary", "Loopback-summary regio", "str"),
    ("region.multi_vrf", "Multi-VRF (ja/nee)", "bool"),
    ("region.lan_summary", "LAN-summary (alleen single-VRF)", "str"),
]
VRF_COLS = ["id", "lan_summary", "dia"]
DEV_COLS = ["type", "name", "serial", "loopback", "isp1_intf", "isp2_intf", "mpls_intf", "wan1_mode", "wan1_ip",
            "wan1_gw", "initial_ip", "isp1_ip", "isp2_ip", "mpls_ip"]
MAX_LANS = 4
_TRUE, _FALSE = {"ja", "j", "true", "1", "aan", "yes", "y", "x"}, {"nee", "n", "false", "0", "uit", "no", ""}


class CsvError(ValueError):
    pass


def _get(t, path):
    for p in path.split("."):
        t = (t or {}).get(p)
    return t


def _set(t, path, v):
    *head, last = path.split(".")
    for p in head:
        t = t.setdefault(p, {})
    t[last] = v


def _fmt(v, kind):
    if kind == "bool":
        return "ja" if v else "nee"
    return "" if v is None else str(v)


def export_csv(topo, delim=";"):
    t = model.merged(topo)
    multi = model._multi(t)
    out = io.StringIO()
    w = csv.writer(out, delimiter=delim, lineterminator="\n")
    w.writerow(["# fgt-sdwan-provision topologie. Vul de lege cellen in (serienummers!) en importeer dit bestand weer."])
    w.writerow(["# Regels met # en lege regels worden genegeerd. Geen tokens of wachtwoorden in dit bestand: die voer je in de tool in."])
    w.writerow([])
    w.writerow(["[algemeen]"])
    w.writerow(["sleutel", "waarde", "uitleg"])
    for key, label, kind in SETTINGS:
        if key == "region.lan_summary" and multi:          # multi-VRF: LAN-summary is per VRF, in [vrf]
            continue
        w.writerow([key, _fmt(_get(t, key), kind), label])
    w.writerow([])
    if multi:
        w.writerow(["# Multi-VRF: lan1_* hoort bij de eerste VRF hieronder, lan2_* bij de tweede, enzovoort."])
        w.writerow(["[vrf]"])
        w.writerow(VRF_COLS)
        for v in t["region"].get("vrfs", []):
            w.writerow([v.get("id", ""), v.get("lan_summary", ""), "ja" if v.get("dia") else "nee"])
        w.writerow([])
    else:
        w.writerow(["# Single-VRF: geen VRF-sectie en per device maar één LAN (lan1_*)."])
        w.writerow([])
    devs = [("hub", d) for d in t["hubs"]] + [("branch", d) for d in t["edges"]]
    nl = min(max(len(t["region"].get("vrfs", [])), 1), MAX_LANS) if multi else 1       # one LAN per VRF / exactly one
    cols = DEV_COLS + [f"lan{i}_{s}" for i in range(1, nl + 1) for s in ("intf", "ip")]
    w.writerow(["[devices]"])
    w.writerow(cols)
    for kind, d in devs:
        row = []
        for c in cols:
            if c == "type":
                row.append(kind)
            elif c.startswith("lan"):
                i, s = int(c[3]) - 1, c.split("_")[1]
                lans = d.get("lans", [])
                row.append(lans[i].get(s, "") if i < len(lans) else "")
            elif c in ("isp1_ip", "isp2_ip", "mpls_ip") and kind != "hub":
                row.append("")
            else:
                row.append(d.get(c, "") or "")
        w.writerow(row)
    return "\ufeff" + out.getvalue()          # BOM: Excel opens UTF-8 correctly


def _rows(text):
    text = text.lstrip("\ufeff")
    lines = [l for l in text.splitlines()]
    head = next((l for l in lines if l.strip() and not l.lstrip().startswith("#") and not re.match(r"\s*\[\w+\]", l)), "")
    delim = ";" if head.count(";") > head.count(",") else ","       # from the first header row, not from values
    for row in csv.reader(lines, delimiter=delim):
        cells = [c.strip() for c in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        yield cells


def _bool(v, where):
    s = v.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise CsvError(f"{where}: '{v}' is geen ja/nee")


def parse_csv(text):
    """-> (partial topology, notes). Raises CsvError with a message that names the offending row/column."""
    notes, sections, cur = [], {}, None
    for cells in _rows(text):
        m = re.fullmatch(r"\[(\w+)\]", cells[0])
        if m:
            cur = m.group(1).lower()
            if cur not in ("algemeen", "vrf", "devices"):
                raise CsvError(f"onbekende sectie [{cur}] (verwacht: [algemeen], [vrf], [devices])")
            sections[cur] = {"header": None, "rows": []}
            continue
        if cur is None:
            raise CsvError("regel buiten een sectie; begin met [algemeen], [vrf] of [devices]")
        s = sections[cur]
        if s["header"] is None:
            s["header"] = [c.lower() for c in cells]
        else:
            s["rows"].append(cells)
    if not sections:
        raise CsvError("geen secties gevonden; download eerst het voorbeeldbestand")

    topo = {}
    kinds = {k: kd for k, _, kd in SETTINGS}
    if "algemeen" in sections:
        for n, r in enumerate(sections["algemeen"]["rows"], start=1):
            key, val = r[0], (r[1] if len(r) > 1 else "")
            if key not in kinds:
                notes.append(f"[algemeen] onbekende sleutel '{key}' genegeerd")
                continue
            kd = kinds[key]
            v = _bool(val, f"[algemeen] {key}") if kd == "bool" else val
            if kd == "int":
                if not re.fullmatch(r"\d+", val):
                    raise CsvError(f"[algemeen] {key}: '{val}' is geen getal")
                v = int(val)
            _set(topo, key, v)
    if "vrf" in sections:
        h, vrfs = sections["vrf"]["header"], []
        for col in ("id", "lan_summary"):
            if col not in h:
                raise CsvError(f"[vrf] kolom '{col}' ontbreekt")
        for n, r in enumerate(sections["vrf"]["rows"], start=1):
            d = dict(zip(h, r + [""] * (len(h) - len(r))))
            if not re.fullmatch(r"\d+", d["id"]):
                raise CsvError(f"[vrf] rij {n}: id '{d['id']}' is geen getal")
            vrfs.append({"id": int(d["id"]), "lan_summary": d["lan_summary"], "dia": _bool(d.get("dia", ""), f"[vrf] rij {n} dia")})
        topo.setdefault("region", {})["vrfs"] = vrfs
    if "devices" in sections:
        h = sections["devices"]["header"]
        for col in ("type", "name"):
            if col not in h:
                raise CsvError(f"[devices] kolom '{col}' ontbreekt")
        hubs, edges, seen = [], [], set()
        for n, r in enumerate(sections["devices"]["rows"], start=1):
            c = dict(zip(h, r + [""] * (len(h) - len(r))))
            kind = {"hub": "hub", "branch": "branch", "edge": "branch", "spoke": "branch"}.get(c["type"].lower())
            if kind is None:
                raise CsvError(f"[devices] rij {n} ({c['name'] or '?'}): type '{c['type']}' moet hub of branch zijn")
            if not c["name"]:
                raise CsvError(f"[devices] rij {n}: naam ontbreekt")
            if c["name"] in seen:
                raise CsvError(f"[devices] naam '{c['name']}' komt dubbel voor")
            seen.add(c["name"])
            d = {"name": c["name"], "serial": c.get("serial", ""), "loopback": c.get("loopback", ""),
                 "isp1_intf": c.get("isp1_intf", ""), "isp2_intf": c.get("isp2_intf", ""), "mpls_intf": c.get("mpls_intf", ""),
                 "wan1_mode": (c.get("wan1_mode") or ("static" if kind == "hub" else "dhcp")).lower(),
                 "wan1_ip": c.get("wan1_ip", ""), "wan1_gw": c.get("wan1_gw", ""),
                 "initial_ip": c.get("initial_ip") or model.DEFAULT_INITIAL_IP}
            if d["wan1_mode"] not in ("static", "dhcp"):
                raise CsvError(f"[devices] {c['name']}: wan1_mode '{d['wan1_mode']}' moet static of dhcp zijn")
            if kind == "hub":
                for k in ("isp1_ip", "isp2_ip", "mpls_ip"):
                    d[k] = c.get(k, "")
                if not d["isp1_ip"] and d["wan1_mode"] == "static" and "/" in d["wan1_ip"]:
                    d["isp1_ip"] = d["wan1_ip"].split("/")[0]          # hub underlay address follows a static wan1
            lans = []
            for i in range(1, MAX_LANS + 1):
                intf, ip = c.get(f"lan{i}_intf", ""), c.get(f"lan{i}_ip", "")
                if intf or ip:
                    lans.append({"intf": intf, "ip": ip})
            d["lans"] = lans
            (hubs if kind == "hub" else edges).append(d)
        topo["hubs"], topo["edges"] = hubs, edges
    single = topo.get("region", {}).get("multi_vrf") is False
    if single:
        if "vrf" in sections:
            notes.append("[vrf] genegeerd: single-VRF gebruikt geen VRF's")
            topo.get("region", {}).pop("vrfs", None)
        extra = [d["name"] for d in topo.get("hubs", []) + topo.get("edges", []) if len(d.get("lans", [])) > 1]
        for d in topo.get("hubs", []) + topo.get("edges", []):
            d["lans"] = d.get("lans", [])[:1]
        if extra:
            notes.append(f"extra LAN's (lan2+) genegeerd bij single-VRF: {', '.join(extra)}")
    elif topo.get("region", {}).get("multi_vrf") is True and "region.lan_summary" in [r[0] for r in sections.get("algemeen", {"rows": []})["rows"]]:
        notes.append("region.lan_summary genegeerd: bij multi-VRF staat de LAN-summary per VRF in [vrf]")
    return topo, notes


def import_csv(text):
    """Parse and merge over the defaults. -> (topology, notes, todo) ; todo = what validation still wants."""
    part, notes = parse_csv(text)
    base = model.merged({})
    for k, v in part.items():
        if k in ("region", "fmgr") and isinstance(v, dict):
            base[k].update(v)
        else:
            base[k] = v
    return base, notes, model.validate(base)
