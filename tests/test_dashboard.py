"""Demo dashboard: layout model, SVG and panels (static/dashboard.js), run in node.  python -m tests.test_dashboard
node is optional (as in test_ui_static): without it the rendering checks are skipped."""
import json, os, shutil, subprocess, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(root, "static", "dashboard.js")).read()
html = open(os.path.join(root, "static", "index.html")).read()
assert html.index('<script src="app.js">') < html.index('<script src="dashboard.js">'), "dashboard.js after app.js"
pure = src[:src.index("// ---- wiring")]                 # the testable part: no DOM at load time

if not shutil.which("node"):
    print("   (node niet gevonden: dashboard-controles overgeslagen)")
    sys.exit(0)


def link(lid, name, seg, ip, **st):
    s = {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0, "rate_kbit": 0, "down": False}
    s.update(st)
    return {"id": lid, "name": name, "seg": seg, "ip": ip, "state": s}


LINKS = [link("site1-H1-wan1", "site1-H1", "hub", "100.64.1.2"), link("site1-H2-wan1", "site1-H2", "hub", "100.64.2.2"),
         link("site1-1-wan1", "site1-1", "a", "100.65.1.2", delay_ms=80, loss_pct=2),
         link("site1-1-wan2", "site1-1", "b", "100.66.1.2", down=True),
         link("site1-2-wan1", "site1-2", "a", "100.65.2.2"),
         link("site1-3-wan1", "site1-3", "a", "100.65.3.2"), link("site1-3-wan2", "site1-3", "b", "100.66.3.2")]
ACCESS = {"fmgs": [{"name": "fmg", "key": "@fmg:fmg", "ip": "10.10.106.251", "gui": "https://10.10.106.251/",
                    "ssh": "ssh admin@10.10.106.251", "password": True}],
          "fgts": [{"name": "site1-1", "ip": "100.65.1.2", "gui": "https://10.10.106.250:10443/",
                    "ssh": "ssh -p 10022 admin@10.10.106.250", "password": True}],
          "clients": [{"name": "client-site1-1", "spoke": "site1-1", "ip": "10.0.1.10", "password": True}]}

PROBE = pure + r"""
const D = JSON.parse(process.argv[1]), out = {};
const m = dashModel(D.links, D.access, D.traffic);
out.m = m;
out.svg = dashSvg(m);
out.labels = [dashLabel({delay_ms: 80, jitter_ms: 0, loss_pct: 2, rate_kbit: 0, down: false}),
              dashLabel({delay_ms: 0, jitter_ms: 0, loss_pct: 0, rate_kbit: 0, down: true}),
              dashLabel({delay_ms: 0, jitter_ms: 0, loss_pct: 0, rate_kbit: 0, down: false}),
              dashLabel({delay_ms: 10, jitter_ms: 5, loss_pct: 0, rate_kbit: 2000, down: false})];
if (typeof dashDevicePanel === "function") {
  out.panels = m.nodes.filter((n) => n.kind === "fgt" || n.kind === "fmg").map((n) => [n.name, dashDevicePanel(n)]);
  out.lpanel = dashLinkPanel(m.edges.find((e) => e.id === "site1-1-wan1"));
}
console.log(JSON.stringify(out));
"""


def run(links, access=ACCESS, traffic=None):
    r = subprocess.run(["node", "-e", PROBE, json.dumps({"links": links, "access": access, "traffic": traffic})],
                       capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


o = run(LINKS)
m = o["m"]
nodes = {n["id"]: n for n in m["nodes"]}
by_name = {n["name"]: n for n in m["nodes"] if n["kind"] == "fgt"}
hubs = [by_name["site1-H1"], by_name["site1-H2"]]
spokes = [by_name[n] for n in ("site1-1", "site1-2", "site1-3")]
clouds = [nodes[c] for c in ("isp-hub", "isp-a", "isp-b")]
assert all(h["role"] == "hub" for h in hubs) and all(s["role"] == "spoke" for s in spokes)
assert max(h["y"] for h in hubs) < min(c["y"] for c in clouds) < max(c["y"] for c in clouds) < min(s["y"] for s in spokes), (hubs, clouds, spokes)
wan = [e for e in m["edges"] if e["kind"] == "wan"]
assert len(wan) == 7, len(wan)
s12 = [e for e in wan if by_name["site1-2"]["id"] in (e["from"], e["to"])]
assert len(s12) == 1 and "isp-a" in (s12[0]["from"], s12[0]["to"]), s12          # one WAN: one line, to ISP-A
st = {e["id"]: e for e in wan}
assert st["site1-1-wan1"]["state"] == "changed" and st["site1-1-wan1"]["label"] == "80 ms · 2 %"
assert st["site1-1-wan2"]["state"] == "down" and st["site1-1-wan2"]["label"] == "down"
assert st["site1-2-wan1"]["state"] == "neutral" and st["site1-2-wan1"]["label"] == ""
assert o["labels"] == ["80 ms · 2 %", "down", "", "10 ms · ±5 ms · 2000 kbit/s"], o["labels"]
assert o["svg"].startswith("<svg") and 'viewBox="0 0 ' in o["svg"]
for lid in st:
    assert f'data-edge="{lid}"' in o["svg"], lid
assert 'id="ic-fgt"' in o["svg"] and o["svg"].count('href="#ic-fgt"') == 5
fm = [n for n in m["nodes"] if n["kind"] == "fmg"]
assert len(fm) == 1 and fm[0]["x"] > max(s["x"] for s in spokes) - 1 and fm[0]["y"] <= hubs[0]["y"], fm
cl = [n for n in m["nodes"] if n["kind"] == "client"]
assert len(cl) == 1 and abs(cl[0]["x"] - by_name["site1-1"]["x"]) <= 1 and cl[0]["y"] > by_name["site1-1"]["y"]
assert any(e["kind"] == "lan" and cl[0]["id"] in (e["from"], e["to"]) for e in m["edges"])
assert [n for n in m["nodes"] if n["kind"] == "wanemu"], "wan-emu drawn when links answer"
assert not [n for n in m["nodes"] if n["kind"] == "tgen"]
assert by_name["site1-1"]["access"]["gui"].endswith(":10443/") and by_name["site1-2"]["access"] is None
print("OK  dashboard: hubs boven, wolken, spokes onder; 7 WAN-lijnen (site1-2 een); labels; FMG/client/wan-emu")

o = run(LINKS, traffic={"deployed": True, "url": "https://10.10.106.252:8443"})
assert len([n for n in o["m"]["nodes"] if n["kind"] == "tgen"]) == 1
o = run(LINKS, access=None, traffic={"deployed": False})
assert all(n["access"] is None for n in o["m"]["nodes"] if n["kind"] == "fgt") and len([n for n in o["m"]["nodes"] if n["kind"] == "fgt"]) == 5

# 8 spokes: all inside the viewBox, spokes at least 100 apart; a name with < and " is escaped
many = LINKS[:2] + [link(f"s{i}-wan1", f"s{i}" if i else 'a<b"c', "a", f"100.65.{i}.2") for i in range(8)]
o = run(many)
m = o["m"]
assert all(0 <= n["x"] <= m["w"] and 0 <= n["y"] <= m["h"] for n in m["nodes"]), m["w"]
xs = sorted(n["x"] for n in m["nodes"] if n.get("role") == "spoke")
assert len(xs) == 8 and min(b - a for a, b in zip(xs, xs[1:])) >= 100, xs
assert 'a<b"c' not in o["svg"] and "a&lt;b&quot;c" in o["svg"]
assert not [n for n in m["nodes"] if n["id"] == "isp-b"], "no ISP-B cloud without ISP-B links"
print("OK  dashboard: 8 spokes passen, namen ge-escaped, Traffic-Gen alleen als uitgerold, zonder access toch getekend")
