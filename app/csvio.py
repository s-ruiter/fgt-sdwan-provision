"""CSV import/export of the *devices* only.

General settings (ADOM, DNS, region, VRF's, multi-VRF, FortiManager, Proxmox ...) are always set in the GUI; the CSV
carries just the hubs and branches, so the same device list can be reused with different settings.

    [devices]
    type;name;serial;loopback;isp1_intf;isp2_intf;mpls_intf;wan1_mode;wan1_ip;wan1_gw;isp1_ip;isp2_ip;mpls_ip;lan1_intf;lan1_ip;...

type = hub | branch. The LAN columns follow the current GUI mode: one pair (lan1_*) for single-VRF, one pair per VRF
for multi-VRF (lan1_* belongs to the first VRF, lan2_* to the second ...). Hub-only columns are empty for branches.
Lines starting with `#` and empty lines are ignored; the delimiter (; or ,) is detected. No tokens or passwords.
"""
import csv
import io
import re

from . import model

DEV_COLS = ["type", "name", "serial", "loopback", "isp1_intf", "isp2_intf", "mpls_intf", "wan1_mode", "wan1_ip",
            "wan1_gw", "isp1_ip", "isp2_ip", "mpls_ip"]
HUB_ONLY = ("isp1_ip", "isp2_ip", "mpls_ip")
MAX_LANS = 4


class CsvError(ValueError):
    pass


def lan_count(t):
    r = t.get("region", {})
    return min(max(len(r.get("vrfs", [])), 1), MAX_LANS) if model._multi(t) else 1


def export_csv(topo, delim=";"):
    t = model.merged(topo)
    multi, nl = model._multi(t), lan_count(t)
    out = io.StringIO()
    w = csv.writer(out, delimiter=delim, lineterminator="\n")
    w.writerow(["# fgt-sdwan-provision: alleen de devices. Algemene instellingen (ADOM, DNS, regio, VRF's, FortiManager ...) vul je in de tool in."])
    w.writerow(["# type = hub of branch. Serienummers zijn verplicht. Geen tokens of wachtwoorden in dit bestand: die voer je in de tool in."])
    w.writerow(["# " + (f"Multi-VRF: lan1_* hoort bij de eerste VRF, lan2_* bij de tweede, enzovoort ({nl} LAN per device)."
                         if multi else "Single-VRF: per device één LAN (lan1_*).")])
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
            elif c in HUB_ONLY and kind != "hub":
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


def parse_devices(text):
    """-> (hubs, edges, notes). Raises CsvError naming the offending row/column."""
    notes, sections, cur = [], {}, None
    for cells in _rows(text):
        m = re.fullmatch(r"\[(\w+)\]", cells[0])
        if m:
            cur = m.group(1).lower()
            sections[cur] = {"header": None, "rows": []}
            if cur != "devices":
                notes.append(f"sectie [{cur}] genegeerd: instellingen doe je in de tool, de CSV bevat alleen [devices]")
            continue
        if cur is None:
            raise CsvError("regel buiten een sectie; begin met [devices]")
        s = sections[cur]
        if s["header"] is None:
            s["header"] = [c.lower() for c in cells]
        else:
            s["rows"].append(cells)
    if "devices" not in sections or sections["devices"]["header"] is None:
        raise CsvError("geen [devices]-sectie gevonden; download eerst het voorbeeldbestand")
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
             "wan1_ip": c.get("wan1_ip", ""), "wan1_gw": c.get("wan1_gw", ""), "initial_ip": model.DEFAULT_INITIAL_IP}
        if d["wan1_mode"] not in ("static", "dhcp"):
            raise CsvError(f"[devices] {c['name']}: wan1_mode '{d['wan1_mode']}' moet static of dhcp zijn")
        if kind == "hub":
            for k in HUB_ONLY:
                d[k] = c.get(k, "")
            if not d["isp1_ip"] and d["wan1_mode"] == "static" and "/" in d["wan1_ip"]:
                d["isp1_ip"] = d["wan1_ip"].split("/")[0]          # hub underlay address follows a static wan1
        d["lans"] = [{"intf": c.get(f"lan{i}_intf", ""), "ip": c.get(f"lan{i}_ip", "")} for i in range(1, MAX_LANS + 1)
                     if c.get(f"lan{i}_intf") or c.get(f"lan{i}_ip")]
        (hubs if kind == "hub" else edges).append(d)
    return hubs, edges, notes


def import_devices(text, current):
    """Merge the devices over the current GUI topology (settings untouched).
    -> (hubs, edges, notes, todo) ; todo = what validation of the merged result still wants."""
    hubs, edges, notes = parse_devices(text)
    base = model.merged(current)
    if not model._multi(base):
        extra = [d["name"] for d in hubs + edges if len(d["lans"]) > 1]
        if extra:
            notes.append(f"extra LAN's (lan2+) genegeerd bij single-VRF: {', '.join(extra)}")
        for d in hubs + edges:
            d["lans"] = d["lans"][:1]
    base["hubs"], base["edges"] = hubs, edges
    return hubs, edges, notes, model.validate(base)
