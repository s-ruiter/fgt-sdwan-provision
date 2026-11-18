"""CSV import/export.

Default file = the *devices* only (`[devices]`): general settings (ADOM, DNS, region, VRF's, multi-VRF, FortiManager,
Proxmox ...) are set in the GUI. A *full* export additionally contains `[algemeen]`, `[vrf]` (multi-VRF) and `[proxmox]`
so a complete deployment can be reused; on import those sections are only applied when the caller asks for it.

    [devices]
    type;name;serial;fortiflex_token;loopback;wan1_intf;wan2_intf;wan1_ip;wan1_gw;lan1_intf;lan1_ip;...

type = hub | branch. WAN design: a hub has one wan (wan1, always static: wan1_ip + wan1_gw); a branch has wan1 and an
optional wan2, both always DHCP. Old columns (wan1_mode, wan1_hub_ip, wan2_ip, a hub's wan2_intf, a branch's static
wan1 address) are ignored with a note. The LAN columns follow the current GUI mode: one pair (lan1_*) for single-VRF, one pair per VRF
for multi-VRF (lan1_* belongs to the first VRF, lan2_* to the second ...). Hub-only columns are empty for branches.
Lines starting with `#` and empty lines are ignored; the delimiter (; or ,) is detected. The FortiFlex tokens
(column fortiflex_token, and fmgr.fortiflex_token in a full export) are in the file, so treat it as a secret; admin
passwords and the Proxmox secret never are.
"""
import csv
import io
import re

from . import model

DEV_COLS = ["type", "name", "serial", "fortiflex_token", "loopback", "wan1_intf", "wan2_intf", "wan1_ip", "wan1_gw"]
HUB_ONLY = ("wan1_ip", "wan1_gw")                 # hub wan1 is static; branch wans are DHCP
EDGE_ONLY = ("wan2_intf",)                         # hubs have a single wan
OLD_WAN = ("wan1_mode", "wan1_hub_ip", "wan2_ip")  # earlier WAN design: ignored
# column names of earlier files; MPLS is no longer used
ALIASES = {"isp1_intf": "wan1_intf", "isp2_intf": "wan2_intf", "isp1_ip": "wan1_hub_ip", "isp2_ip": "wan2_ip"}
DROPPED = ("mpls_intf", "mpls_ip")
_TOKEN = re.compile(r"^[A-Za-z0-9]{10,64}$")
MAX_LANS = 4

# (dotted key into the topology, explanation, kind)   kind: str | int | bool | optint (blank = none)
SETTINGS = [
    ("adom", "ADOM-naam", "str"),
    ("os_ver", "ADOM-versie (7.6 of 8.0)", "str"),
    ("hub2hub", "Hub-naar-hub tunnels (ja/nee)", "bool"),
    ("fmgr.name", "FortiManager hostname", "str"),
    ("fmgr.ip", "FortiManager IP/prefix, bv. 10.0.0.5/24", "str"),
    ("fmgr.gateway", "FortiManager gateway", "str"),
    ("fmgr.port", "FortiManager poort (ZTP)", "str"),
    ("fmgr.fortiflex_token", "FortiFlex-token van de FortiManager (GEHEIM: behandel dit bestand als een wachtwoord)", "str"),
    ("region.multi_vrf", "Multi-VRF (ja/nee)", "bool"),
    ("region.lan_summary", "LAN-summary (alleen single-VRF)", "str"),
]
SETTINGS_LABEL = {k: lbl for k, lbl, _ in SETTINGS}
PVE_SETTINGS = [
    ("pve.server", "Naam van de opgeslagen Proxmox-server (pagina Servers)", "str"),
    ("pve.node", "Node", "str"), ("pve.disk_storage", "Disk-storage", "str"),
    ("pve.import_storage", "Import-storage", "str"), ("pve.iso_storage", "ISO-storage", "str"),
    ("pve.default_bridge", "Standaard bridge", "str"), ("pve.cpu_type", "CPU-type", "str"),
    ("pve.fmg.cores", "FortiManager vCPU", "int"), ("pve.fmg.memory_mb", "FortiManager RAM (MB)", "int"),
    ("pve.fmg.data_disk_gb", "FortiManager datadisk (GB)", "int"), ("pve.fmg.bridge", "FortiManager bridge", "str"),
    ("pve.fmg.vlan", "FortiManager VLAN", "optint"),
    ("pve.fgt.cores", "FortiGate vCPU", "int"), ("pve.fgt.memory_mb", "FortiGate RAM (MB)", "int"),
] + [(f"pve.ports.port{i}.{k}", f"FortiGate port{i} {k}", "optint" if k == "vlan" else "str")
     for i in range(1, 9) for k in ("bridge", "vlan")]
VRF_COLS = ["id", "lan_summary", "dia"]
_TRUE, _FALSE = {"ja", "j", "true", "1", "aan", "yes", "y", "x"}, {"nee", "n", "false", "0", "uit", "no", ""}


class CsvError(ValueError):
    pass


def _get(t, path):
    for p in path.split("."):
        t = (t or {}).get(p) if isinstance(t, dict) else None
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


def _typed(val, kind, where):
    if kind == "bool":
        s = val.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise CsvError(f"{where}: '{val}' is geen ja/nee")
    if kind in ("int", "optint"):
        if kind == "optint" and val == "":
            return None
        if not re.fullmatch(r"\d+", val):
            raise CsvError(f"{where}: '{val}' is geen getal")
        return int(val)
    return val


def lan_count(t):
    r = t.get("region", {})
    return min(max(len(r.get("vrfs", [])), 1), MAX_LANS) if model._multi(t) else 1


def export_csv(topo, delim=";", full=False, server_name="", tokens=None):
    """full=True: also [algemeen], [vrf] (multi-VRF) and [proxmox], so a whole deployment can be reused.
    tokens = {"fmg": "...", "devices": {name: token}} (FortiFlex tokens typed in the GUI)."""
    t = model.merged(topo)
    tokens = tokens or {}
    t["fmgr"]["fortiflex_token"] = tokens.get("fmg", "") or ""
    multi, nl = model._multi(t), lan_count(t)
    out = io.StringIO()
    w = csv.writer(out, delimiter=delim, lineterminator="\n")
    if full:
        w.writerow(["# fgt-sdwan-provision: volledige export (instellingen + devices). Importeerbaar voor een volgende uitrol."])
    else:
        w.writerow(["# fgt-sdwan-provision: alleen de devices. Algemene instellingen (ADOM, DNS, regio, VRF's, FortiManager ...) vul je in de tool in."])
    w.writerow(["# type = hub of branch. Serienummers zijn verplicht. De FortiFlex-tokens staan wel in dit bestand: behandel het als geheim. Admin-wachtwoorden staan er nooit in."])
    w.writerow(["# " + (f"Multi-VRF: lan1_* hoort bij de eerste VRF, lan2_* bij de tweede, enzovoort ({nl} LAN per device)."
                         if multi else "Single-VRF: per device één LAN (lan1_*).")])
    w.writerow([])
    if full:
        w.writerow(["[algemeen]"])
        w.writerow(["sleutel", "waarde", "uitleg"])
        for key, label, kind in SETTINGS:
            if key == "region.lan_summary" and multi:
                continue
            w.writerow([key, _fmt(_get(t, key), kind), label])
        w.writerow([])
        if multi:
            w.writerow(["[vrf]"])
            w.writerow(VRF_COLS)
            for v in t["region"].get("vrfs", []):
                w.writerow([v.get("id", ""), v.get("lan_summary", ""), "ja" if v.get("dia") else "nee"])
            w.writerow([])
        w.writerow(["[proxmox]"])
        w.writerow(["sleutel", "waarde", "uitleg"])
        t["pve"]["server"] = server_name
        for key, label, kind in PVE_SETTINGS:
            w.writerow([key, _fmt(_get(t, key), kind), label])
        w.writerow([])
    cols = DEV_COLS + [f"lan{i}_{s}" for i in range(1, nl + 1) for s in ("intf", "ip")]
    w.writerow(["[devices]"])
    w.writerow(cols)
    for kind, d in [("hub", d) for d in t["hubs"]] + [("branch", d) for d in t["edges"]]:
        row = []
        for c in cols:
            if c == "type":
                row.append(kind)
            elif c.startswith("lan"):
                i, s = int(c[3]) - 1, c.split("_")[1]
                lans = d.get("lans", [])
                row.append(lans[i].get(s, "") if i < len(lans) else "")
            elif c == "fortiflex_token":
                row.append((tokens.get("devices") or {}).get(d.get("name"), "") or "")
            elif (c in HUB_ONLY and kind != "hub") or (c in EDGE_ONLY and kind == "hub"):
                row.append("")
            else:
                row.append(d.get(c, "") or "")
        w.writerow(row)
    return "\ufeff" + out.getvalue()          # BOM: Excel opens UTF-8 correctly


def _rows(text):
    lines = text.lstrip("\ufeff").splitlines()
    head = next((l for l in lines if l.strip() and not l.lstrip().startswith("#") and not re.match(r"\s*\[\w+\]", l)), "")
    delim = ";" if head.count(";") > head.count(",") else ","       # from the first header row, not from values
    for row in csv.reader(lines, delimiter=delim):
        cells = [c.strip() for c in row]
        if any(cells) and not cells[0].startswith("#"):
            yield cells


def _parse_settings(sections):
    """[algemeen], [vrf], [proxmox] -> partial topology dict (pve.server becomes pve.server_name)."""
    out, kinds = {}, {k: kd for k, _, kd in SETTINGS + PVE_SETTINGS}
    notes = []
    for name in ("algemeen", "proxmox"):
        for r in sections.get(name, {"rows": []})["rows"]:
            key, val = r[0], (r[1] if len(r) > 1 else "")
            if key not in kinds:
                notes.append(f"[{name}] onbekende sleutel '{key}' genegeerd")
                continue
            _set(out, "pve.server_name" if key == "pve.server" else key, _typed(val, kinds[key], f"[{name}] {key}"))
    if "vrf" in sections:
        h, vrfs = sections["vrf"]["header"] or [], []
        for col in ("id", "lan_summary"):
            if col not in h:
                raise CsvError(f"[vrf] kolom '{col}' ontbreekt")
        for n, r in enumerate(sections["vrf"]["rows"], start=1):
            d = dict(zip(h, r + [""] * (len(h) - len(r))))
            vrfs.append({"id": _typed(d["id"], "int", f"[vrf] rij {n} id"), "lan_summary": d["lan_summary"],
                         "dia": _typed(d.get("dia", ""), "bool", f"[vrf] rij {n} dia")})
        out.setdefault("region", {})["vrfs"] = vrfs
    return out, notes


def parse_devices(text, with_settings=False):
    """-> (hubs, edges, notes, settings, has_settings, tokens). settings is {} unless with_settings.
    tokens = {device name: FortiFlex token}. Raises CsvError naming the row/column."""
    notes, sections, cur = [], {}, None
    for cells in _rows(text):
        m = re.fullmatch(r"\[(\w+)\]", cells[0])
        if m:
            cur = m.group(1).lower()
            sections[cur] = {"header": None, "rows": []}
            if cur not in ("devices", "algemeen", "vrf", "proxmox"):
                raise CsvError(f"onbekende sectie [{cur}] (verwacht: [devices], optioneel [algemeen], [vrf], [proxmox])")
            continue
        if cur is None:
            raise CsvError("regel buiten een sectie; begin met [devices]")
        s = sections[cur]
        if s["header"] is None:
            s["header"] = [ALIASES.get(c.lower(), c.lower()) if cur == "devices" else c.lower() for c in cells]
        else:
            s["rows"].append(cells)
    if "devices" not in sections or sections["devices"]["header"] is None:
        raise CsvError("geen [devices]-sectie gevonden; download eerst het voorbeeldbestand")
    settings = {}
    has_settings = any(k in sections for k in ("algemeen", "vrf", "proxmox"))
    if with_settings:
        settings, n2 = _parse_settings(sections)
        notes += n2
    elif has_settings:
        notes.append("instellingen in het bestand genegeerd (alleen de devices zijn overgenomen)")
    h = sections["devices"]["header"]
    for col in ("type", "name"):
        if col not in h:
            raise CsvError(f"[devices] kolom '{col}' ontbreekt")
    hubs, edges, seen, tokens = [], [], set(), {}
    if any(col in h for col in DROPPED):
        notes.append("MPLS-kolommen genegeerd (MPLS wordt niet meer gebruikt)")
    old = [col for col in OLD_WAN if col in h]
    if old:
        notes.append(f"kolom(men) {', '.join(old)} genegeerd: hubs hebben altijd een statisch wan1, branches altijd DHCP")
    wan_notes = set()
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
             "wan1_intf": c.get("wan1_intf", ""), "initial_ip": model.DEFAULT_INITIAL_IP}
        if kind == "hub":
            d.update(wan1_mode="static", wan1_ip=c.get("wan1_ip", ""), wan1_gw=c.get("wan1_gw", ""))
            if c.get("wan2_intf"):
                wan_notes.add("wan2_intf van hubs genegeerd (een hub heeft alleen wan1)")
        else:
            d.update(wan1_mode="dhcp", wan2_intf=c.get("wan2_intf", ""))
            if c.get("wan1_ip") or c.get("wan1_gw"):
                wan_notes.add("wan1_ip/wan1_gw van branches genegeerd (branches gebruiken altijd DHCP)")
        tok = c.get("fortiflex_token", "")
        if tok:
            if not _TOKEN.match(tok):
                raise CsvError(f"[devices] {c['name']}: fortiflex_token ongeldig (10-64 letters/cijfers)")
            tokens[c["name"]] = tok
        d["lans"] = [{"intf": c.get(f"lan{i}_intf", ""), "ip": c.get(f"lan{i}_ip", "")} for i in range(1, MAX_LANS + 1)
                     if c.get(f"lan{i}_intf") or c.get(f"lan{i}_ip")]
        (hubs if kind == "hub" else edges).append(d)
    notes += sorted(wan_notes)
    return hubs, edges, notes, settings, has_settings, tokens


def import_devices(text, current, apply_settings=False):
    """Devices over the current GUI topology; with apply_settings the [algemeen]/[vrf]/[proxmox] sections too.
    -> dict(hubs, edges, notes, todo, settings, has_settings); todo = what validation of the merged result still wants."""
    hubs, edges, notes, settings, has_settings, tokens = parse_devices(text, apply_settings)
    fmg_token = (settings.get("fmgr") or {}).pop("fortiflex_token", "")      # a secret, not part of the topology
    if fmg_token and not _TOKEN.match(fmg_token):
        raise CsvError("[algemeen] fmgr.fortiflex_token ongeldig (10-64 letters/cijfers)")
    base = model.merged(current)
    if settings:
        model._deep(base, {k: v for k, v in settings.items() if k != "pve"})
        model._deep(base["pve"], {k: v for k, v in settings.get("pve", {}).items() if k != "server_name"})
    if not model._multi(base):
        extra = [d["name"] for d in hubs + edges if len(d["lans"]) > 1]
        if extra:
            notes.append(f"extra LAN's (lan2+) genegeerd bij single-VRF: {', '.join(extra)}")
        for d in hubs + edges:
            d["lans"] = d["lans"][:1]
    base["hubs"], base["edges"] = hubs, edges
    return {"hubs": hubs, "edges": edges, "notes": notes, "todo": model.validate(base), "settings": settings,
            "has_settings": has_settings, "tokens": tokens, "fmg_token": fmg_token}
