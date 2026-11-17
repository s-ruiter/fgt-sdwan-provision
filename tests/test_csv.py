"""CSV (devices only): export, import over the current GUI settings, errors.  python -m tests.test_csv"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import csvio, model


def topo(multi):
    t = model.merged({})
    t["region"]["multi_vrf"] = multi
    t["adom"], t["dns_primary"], t["psk"] = "ACME", "1.1.1.1", "Lab;Psk,1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.64.1.6", "100.64.2.6"
    return t


def cols(text):
    return next(l for l in text.splitlines() if l.startswith("type"))


m, s = topo(True), topo(False)
em, es = csvio.export_csv(m), csvio.export_csv(s)
# only [devices]; nothing from the GUI settings, no secrets
for txt in (em, es):
    body = "\n".join(l for l in txt.splitlines() if not l.lstrip("\ufeff").startswith("#")).lower()
    assert txt.startswith("\ufeff") and "[devices]" in txt and "[algemeen]" not in txt and "[vrf]" not in txt
    assert not any(w in body for w in ("acme", "lab;psk", "1.1.1.1", "adom", "dns", "psk", "token", "secret", "wachtwoord", "password"))
    assert "initial_ip" not in txt
# LAN columns follow the mode
assert "lan2_intf" in cols(em) and "lan3_intf" not in cols(em)
assert "lan1_intf" in cols(es) and "lan2_intf" not in cols(es)
three = dict(m, region=dict(m["region"], vrfs=m["region"]["vrfs"] + [{"id": 13, "lan_summary": "10.0.0.0/14", "dia": False}]))
assert "lan3_intf" in cols(csvio.export_csv(three))

# round-trip over the GUI settings, both delimiters; settings are not touched (import returns devices only)
for delim in (";", ","):
    hubs, edges, notes, todo = csvio.import_devices(csvio.export_csv(m, delim), m)
    assert [d["name"] for d in hubs] == ["site1-H1", "site1-H2"] and len(edges) == 3 and notes == [] and todo == [], (todo, notes)
    assert hubs[0]["wan1_gw"] == "100.64.1.6" and hubs[0]["isp2_ip"] == m["hubs"][0]["isp2_ip"] and "isp2_ip" not in edges[0]
    assert edges[1]["isp2_intf"] == "" and edges[0]["lans"] == m["edges"][0]["lans"]
# single-VRF GUI + multi-VRF layout in the file: extra LAN's dropped with a note
hubs, edges, notes, todo = csvio.import_devices(em, s)
assert all(len(d["lans"]) <= 1 for d in hubs + edges) and any("lan2+" in n for n in notes), notes
# multi-VRF GUI with a single-VRF file: lan2 missing -> validation todo is only advisory, import still succeeds
hubs, edges, notes, todo = csvio.import_devices(es, m)
assert len(edges) == 3
# [algemeen]/[vrf] in an old file are ignored with a note
old = "[algemeen]\nsleutel;waarde\nadom;HACK\n[vrf]\nid;lan_summary\n99;10.0.0.0/8\n" + em.split("[devices]")[0].count("x") * "" + "[devices]" + em.split("[devices]")[1]
hubs, edges, notes, todo = csvio.import_devices(old, m)
assert sum("genegeerd" in n for n in notes) == 2 and len(hubs) == 2
# blank template -> serials missing is a todo, not an error
h2, e2, _, todo = csvio.import_devices(csvio.export_csv(model.merged({})), model.merged({}))
assert any("serienummer" in e for e in todo) and len(h2) == 2
# isp1_ip derived, aliases, Excel-ish whitespace
h3, e3, _, _ = csvio.import_devices("[devices]\ntype;name;serial;wan1_mode;wan1_ip\n Hub ;H;X;static;100.64.1.1/29\nspoke;E;Y;;\n", m)
assert h3[0]["isp1_ip"] == "100.64.1.1" and e3[0]["wan1_mode"] == "dhcp"
# errors name the row/column
for bad, needle in [("", "[devices]"), ("[algemeen]\nk;v\n", "[devices]"), ("a;b\n", "buiten een sectie"),
                    ("[devices]\ntype;name\nrouter;A\n", "type 'router'"), ("[devices]\ntype;name\nhub;A\nhub;A\n", "dubbel"),
                    ("[devices]\nname\nA\n", "kolom 'type'"), ("[devices]\ntype;name;wan1_mode\nhub;A;pppoe\n", "static of dhcp")]:
    try:
        csvio.import_devices(bad, m)
        raise SystemExit(f"geen fout voor {bad!r}")
    except csvio.CsvError as e:
        assert needle in str(e), (bad, str(e))
print("OK  CSV (alleen [devices]): export zonder instellingen/geheimen, LAN-kolommen per modus, import raakt de GUI-instellingen niet, oude secties genegeerd, fouten per rij")
