"""CSV (devices only): export, import over the current GUI settings, errors.  python -m tests.test_csv"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import csvio, model


def topo(multi):
    t = model.merged({})
    t["region"]["multi_vrf"] = multi
    t["adom"] = "ACME"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.64.1.6", "100.64.2.6"
    return t


def imp(text, cur, apply=False):
    r = csvio.import_devices(text, cur, apply)
    return r["hubs"], r["edges"], r["notes"], r["todo"]


def cols(text):
    return next(l for l in text.splitlines() if l.startswith("type"))


m, s = topo(True), topo(False)
em, es = csvio.export_csv(m), csvio.export_csv(s)
# only [devices]; nothing from the GUI settings, no secrets
for txt in (em, es):
    body = "\n".join(l for l in txt.splitlines() if not l.lstrip("\ufeff").startswith("#")).lower()
    assert txt.startswith("\ufeff") and "[devices]" in txt and "[algemeen]" not in txt and "[vrf]" not in txt
    assert not any(w in body for w in ("acme", "adom", "dns", "psk", "secret", "wachtwoord", "password"))
    assert "initial_ip" not in txt
# LAN columns follow the mode
assert "lan2_intf" in cols(em) and "lan3_intf" not in cols(em)
assert "lan1_intf" in cols(es) and "lan2_intf" not in cols(es)
three = dict(m, region=dict(m["region"], vrfs=m["region"]["vrfs"] + [{"id": 13, "lan_summary": "10.0.0.0/14", "dia": False}]))
assert "lan3_intf" in cols(csvio.export_csv(three))

# round-trip over the GUI settings, both delimiters; settings are not touched (import returns devices only)
for delim in (";", ","):
    hubs, edges, notes, todo = imp(csvio.export_csv(m, delim), m)
    assert [d["name"] for d in hubs] == ["site1-H1", "site1-H2"] and len(edges) == 3 and notes == [] and todo == [], (todo, notes)
    assert hubs[0]["wan1_gw"] == "100.64.1.6" and hubs[0]["wan1_ip"] == m["hubs"][0]["wan1_ip"]
    assert "wan2_intf" not in hubs[0] and "wan1_ip" not in edges[0] and edges[0]["wan2_intf"] == "port2"   # hub: 1 wan; branch: DHCP
    assert edges[1]["wan2_intf"] == "" and edges[0]["lans"] == m["edges"][0]["lans"]
# single-VRF GUI + multi-VRF layout in the file: extra LAN's dropped with a note
hubs, edges, notes, todo = imp(em, s)
assert all(len(d["lans"]) <= 1 for d in hubs + edges) and any("lan2+" in n for n in notes), notes
# multi-VRF GUI with a single-VRF file: lan2 missing -> validation todo is only advisory, import still succeeds
hubs, edges, notes, todo = imp(es, m)
assert len(edges) == 3
# [algemeen]/[vrf] in an old file are ignored with a note
old = "[algemeen]\nsleutel;waarde\nadom;HACK\n[vrf]\nid;lan_summary\n99;10.0.0.0/8\n" + em.split("[devices]")[0].count("x") * "" + "[devices]" + em.split("[devices]")[1]
hubs, edges, notes, todo = imp(old, m)
assert sum("genegeerd" in n for n in notes) == 1 and len(hubs) == 2
# blank template -> serials missing is a todo, not an error
h2, e2, _, todo = imp(csvio.export_csv(model.merged({})), model.merged({}))
assert any("serienummer" in e for e in todo) and len(h2) == 2
# WAN design is forced: hub wan1 static, branch DHCP; old columns ignored with a note; Excel-ish whitespace
h3, e3, n3, _ = imp("[devices]\ntype;name;serial;wan1_mode;wan1_ip;wan2_intf\n Hub ;H;X;dhcp;100.64.1.1/29;port2\nspoke;E;Y;static;100.64.9.1/29;port2\n", m)
assert h3[0]["wan1_mode"] == "static" and h3[0]["wan1_ip"] == "100.64.1.1/29" and "wan2_intf" not in h3[0]
assert e3[0]["wan1_mode"] == "dhcp" and "wan1_ip" not in e3[0] and e3[0]["wan2_intf"] == "port2"
assert any("wan1_mode" in n for n in n3) and any("hubs genegeerd" in n for n in n3) and any("branches genegeerd" in n for n in n3), n3
# errors name the row/column
for bad, needle in [("", "[devices]"), ("[algemeen]\nk;v\n", "[devices]"), ("[bogus]\n", "onbekende sectie"), ("a;b\n", "buiten een sectie"),
                    ("[devices]\ntype;name\nrouter;A\n", "type 'router'"), ("[devices]\ntype;name\nhub;A\nhub;A\n", "dubbel"),
                    ("[devices]\nname\nA\n", "kolom 'type'")]:
    try:
        imp(bad, m)
        raise SystemExit(f"geen fout voor {bad!r}")
    except csvio.CsvError as e:
        assert needle in str(e), (bad, str(e))
# ---- full export: settings + devices, importable for a next deployment -------------------------------------------
for multi in (True, False):
    g = topo(multi)
    g["pve"].update(node="pve01", disk_storage="local-lvm", import_storage="local", iso_storage="local", default_bridge="vmbr0", server_id=7)
    g["pve"]["fmg"].update(cores=6, bridge="vmbr1", vlan=106)
    g["pve"]["ports"]["port1"].update(bridge="vmbr0", vlan=20)
    g["hub2hub"] = True
    if multi:
        g["region"]["vrfs"][1]["dia"] = True
    full = csvio.export_csv(g, full=True, server_name="pve01")
    assert all(x in full for x in ("[algemeen]", "[proxmox]", "[devices]")) and ("[vrf]" in full) == multi
    assert "region.lan_summary" in full.split("[proxmox]")[0] or multi
    body_full = "\n".join(l for l in full.splitlines() if not l.startswith("#"))
    assert [l for l in body_full.splitlines() if l.startswith("fmgr.fortiflex_token;")] == ["fmgr.fortiflex_token;;" + csvio.SETTINGS_LABEL["fmgr.fortiflex_token"]]   # key present, value empty
    assert not any(w in body_full.replace(csvio.SETTINGS_LABEL["fmgr.fortiflex_token"], "").lower() for w in ("secret", "password", "wachtwoord"))
    # devices-only import ignores the settings (note), full import applies them
    r = csvio.import_devices(full, topo(not multi))
    assert r["settings"] == {} and r["has_settings"] and any("genegeerd" in n for n in r["notes"])
    r = csvio.import_devices(full, topo(not multi), True)
    st = r["settings"]
    assert st["adom"] == "ACME" and st["hub2hub"] is True
    assert st["region"]["multi_vrf"] is multi and st["fmgr"]["ip"] == "10.0.0.5/24"
    assert st["pve"]["server_name"] == "pve01" and st["pve"]["node"] == "pve01" and st["pve"]["fmg"]["cores"] == 6
    assert st["pve"]["fmg"]["vlan"] == 106 and st["pve"]["ports"]["port1"]["vlan"] == 20 and st["pve"]["ports"]["port2"]["vlan"] is None
    assert ("vrfs" in st["region"]) == multi and (not multi or st["region"]["vrfs"][1]["dia"] is True)
    assert r["todo"] == [], r["todo"]          # validated against the *imported* settings (mode matches the LAN columns)
    assert [d["name"] for d in r["hubs"]] == ["site1-H1", "site1-H2"] and len(r["edges"]) == 3
    assert "server_id" not in str(st)           # DB ids are not portable: only the server name travels
# fixed values: not in the file, and an imported/old file cannot change them
full = csvio.export_csv(topo(True), full=True)
for k in ("platform", "dns_primary", "dns_secondary", "wan1_access", "psk", "lo_summary", "region.name", "region.bgp_as", "region.lo_summary", "pve.license_mode"):
    assert f"\n{k};" not in full, k
old = full.replace("hub2hub;nee", "hub2hub;nee\ndns_primary;4.4.4.4\npsk;ikkiezzelf\nregion.bgp_as;1")
r = csvio.import_devices(old, topo(True), True)
assert any("onbekende sleutel" in n for n in r["notes"]) and "dns_primary" not in r["settings"] and "psk" not in r["settings"]
fx = model.merged({"dns_primary": "4.4.4.4", "dns_secondary": "", "wan1_access": "x", "platform": "p", "region": {"name": "Oost", "bgp_as": 1, "lo_summary": "1.2.3.0/24", "multi_vrf": False}})
assert (fx["dns_primary"], fx["dns_secondary"], fx["wan1_access"], fx["platform"]) == ("1.1.1.1", "8.8.8.8", "ping https ssh fgfm", "FortiGate-VM64")
assert (fx["region"]["name"], fx["region"]["bgp_as"], fx["region"]["lo_summary"]) == ("West", 65001, "10.200.1.0/24") and fx["region"]["multi_vrf"] is False
# a bad value in a settings section only matters when settings are applied
bad = csvio.export_csv(topo(True), full=True, server_name="x").replace("pve.fmg.cores;4", "pve.fmg.cores;abc")
assert imp(bad, topo(True))[0]
try:
    csvio.import_devices(bad, topo(True), True)
    raise SystemExit("geen fout")
except csvio.CsvError as e:
    assert "geen getal" in str(e) and "pve.fmg.cores" in str(e)
# ---- WAN naming, no MPLS, FortiFlex tokens ----------------------------------------------------------------------
for bad in ("isp", "mpls", "MPLS"):
    assert bad not in em + es, bad
assert "wan1_intf" in cols(em) and "wan2_intf" in cols(em) and "fortiflex_token" in cols(em)
assert not any(c in cols(em) for c in ("wan2_ip", "wan1_mode", "wan1_hub_ip"))
# earlier files (isp*/mpls* columns) still import: aliases + a note about the dropped MPLS columns
oldf = "[devices]\ntype;name;serial;loopback;isp1_intf;isp2_intf;mpls_intf;wan1_mode;wan1_ip;wan1_gw;isp1_ip;isp2_ip;mpls_ip;lan1_intf;lan1_ip\n" \
       "hub;H1;FGVMMLTM260000001;10.200.1.253;port1;port2;port4;static;100.64.1.1/29;100.64.1.6;100.64.1.1;100.64.1.9;172.16.1.5;port5;10.1.0.1/24\n"
r = csvio.import_devices(oldf, s)
assert r["hubs"][0]["wan1_intf"] == "port1" and r["hubs"][0]["wan1_ip"] == "100.64.1.1/29" and "wan2_intf" not in r["hubs"][0]
assert "mpls_intf" not in r["hubs"][0] and any("MPLS" in n for n in r["notes"]) and any("wan2_ip" in n for n in r["notes"])
# tokens: in the file (devices column + FMG in a full export), validated, returned separately, never in the topology
tk = {"fmg": "FMGTOKEN0123456789", "devices": {"site1-H1": "FGTTOKENHUB1ABCDEF", "site1-3": "FGTTOKENSITE3ABCDE"}}
for full in (False, True):
    txt = csvio.export_csv(m, full=full, tokens=tk)
    assert "FGTTOKENHUB1ABCDEF" in txt and "FGTTOKENSITE3ABCDE" in txt and ("FMGTOKEN0123456789" in txt) == full
    r = csvio.import_devices(txt, m, True)
    assert r["tokens"] == tk["devices"] and r["fmg_token"] == ("FMGTOKEN0123456789" if full else ""), (r["tokens"], r["fmg_token"])
    assert "token" not in str(r["settings"]).lower() and "token" not in str(r["hubs"] + r["edges"]).lower()   # not part of the topology
    assert not any(w in "\n".join(l for l in txt.splitlines() if not l.startswith("#")).replace(csvio.SETTINGS_LABEL["fmgr.fortiflex_token"], "").lower()
                   for w in ("secret", "password", "wachtwoord"))
assert csvio.import_devices(csvio.export_csv(m, tokens=tk), m)["tokens"] == tk["devices"]          # also without opt-in: devices
try:
    csvio.import_devices(csvio.export_csv(m, tokens={"devices": {"site1-H1": "kort"}}), m)
    raise SystemExit("geen fout voor ongeldig token")
except csvio.CsvError as e:
    assert "site1-H1" in str(e) and "fortiflex_token" in str(e)
print("OK  CSV (alleen [devices]): export zonder instellingen/geheimen, LAN-kolommen per modus, import raakt de GUI-instellingen niet, oude secties genegeerd, fouten per rij; volledige export (algemeen/vrf/proxmox) round-trip, alleen met opt-in")
