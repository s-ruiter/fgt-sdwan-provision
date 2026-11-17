"""CSV export/import round-trip and error handling (no servers).  python -m tests.test_csv"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import csvio, model

t = model.merged({})
t["adom"], t["dns_primary"], t["psk"] = "ACME", "1.1.1.1", "Lab;Psk,1"          # delimiter characters in a value
t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
t["region"]["multi_vrf"] = False
t["region"]["lan_summary"] = "10.0.0.0/8"
for i, d in enumerate(t["hubs"] + t["edges"]):
    d["serial"] = f"FGVMMLTM2600{i:04d}"
t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.64.1.6", "100.64.2.6"

txt = csvio.export_csv(t)
assert txt.startswith("\ufeff") and "[algemeen]" in txt and "[vrf]" in txt and "[devices]" in txt
body = "\n".join(l for l in txt.splitlines() if not l.lstrip("\ufeff").startswith("#")).lower()
assert not any(w in body for w in ("token", "secret", "wachtwoord", "password"))
# round-trip (both delimiters)
for delim in (";", ","):
    back, notes, todo = csvio.import_csv(csvio.export_csv(t, delim))
    for k in ("adom", "dns_primary", "psk", "os_ver", "platform", "hub2hub", "lo_summary", "wan1_access"):
        assert back[k] == t[k], (k, back[k], t[k])
    assert back["fmgr"]["ip"] == "10.0.0.5/24" and back["region"]["multi_vrf"] is False and back["region"]["bgp_as"] == 65001
    assert back["region"]["vrfs"] == t["region"]["vrfs"]
    assert [d["name"] for d in back["hubs"]] == ["site1-H1", "site1-H2"] and len(back["edges"]) == 3
    assert back["hubs"][0]["wan1_gw"] == "100.64.1.6" and back["hubs"][0]["isp2_ip"] == t["hubs"][0]["isp2_ip"]
    assert back["edges"][1]["isp2_intf"] == "" and back["edges"][0]["lans"] == t["edges"][0]["lans"]
    assert "isp2_ip" not in back["edges"][0] and todo == [] and notes == [], (todo, notes)
# blank template: serials missing -> todo list, not an error
blank, _, todo = csvio.import_csv(csvio.export_csv(model.merged({})))
assert any("serienummer" in e for e in todo) and len(blank["hubs"]) == 2
# Excel-ish: cp1252-style, extra spaces, 'Ja', unknown key, branch alias
x = "[algemeen]\nsleutel;waarde\n adom ; X1 \nregion.multi_vrf;Ja\nfoo.bar;1\n[devices]\ntype;name;serial;isp1_intf\nhub;H1;FGVMMLTM260000001;port1\nspoke;E1;FGVMMLTM260000002;port1\n"
b, notes, _ = csvio.import_csv(x)
assert b["adom"] == "X1" and b["region"]["multi_vrf"] is True and notes and "foo.bar" in notes[0]
assert b["hubs"][0]["wan1_mode"] == "static" and b["edges"][0]["wan1_mode"] == "dhcp"
b2, _, td = csvio.import_csv("[devices]\ntype;name;serial;wan1_mode;wan1_ip\nhub;H;X;static;100.64.1.1/29\n")
assert b2["hubs"][0]["isp1_ip"] == "100.64.1.1"
# errors name the row/column
for bad, needle in [("", "geen secties"), ("[x]\n", "onbekende sectie"), ("a;b\n", "buiten een sectie"),
                    ("[devices]\ntype;name\nrouter;A\n", "type 'router'"), ("[devices]\ntype;name\nhub;A\nhub;A\n", "dubbel"),
                    ("[algemeen]\nk;v\nregion.bgp_as;abc\n", "geen getal"), ("[algemeen]\nk;v\nhub2hub;misschien\n", "ja/nee"),
                    ("[vrf]\nid;lan_summary\nx;10.0.0.0/8\n", "geen getal"), ("[devices]\nname\nA\n", "kolom 'type'")]:
    try:
        csvio.import_csv(bad)
        raise SystemExit(f"geen fout voor {bad!r}")
    except csvio.CsvError as e:
        assert needle in str(e), (bad, str(e))
print("OK  CSV: export zonder geheimen, round-trip (; en ,), blanco -> todo, Excel-varianten, duidelijke fouten")
