"""Static UI checks (no browser): app.js parses, the start-up line only calls functions that exist, and every
element id used via $("#id") exists in index.html.  python -m tests.test_ui_static"""
import os, re, shutil, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
js = open(os.path.join(root, "static", "app.js")).read()
html = open(os.path.join(root, "static", "index.html")).read()

# 1. syntax (node is optional)
try:
    r = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(process.argv[1],'utf8'))", os.path.join(root, "static", "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
except FileNotFoundError:
    print("   (node niet gevonden: syntaxcontrole overgeslagen)")

# 2. the start-up line: every called function is defined (a missing one aborts the whole page init)
init = "\n".join(l for l in js.splitlines() if "await serversLoad(true)" in l or "stepsDraw();" in l and "markIpFields();" in l)
assert "imgLoad" in init and "route()" in init, "start-up line not found"
for name in re.findall(r"\b([A-Za-z_]\w*)\(", init):
    if name in ("route",) or re.search(rf"(function\s+{name}\b|const\s+{name}\b|let\s+{name}\b|var\s+{name}\b)", js):
        continue
    raise SystemExit(f"start-up roept {name}() aan maar die bestaat niet in app.js")

# 3. ids used by the script exist in the page (ids created dynamically are listed here)
dynamic = {"tabs", "cfg", "preview"}                       # present in html anyway; kept for clarity
ids = set(re.findall(r'\$\("#([A-Za-z_][\w-]*)"\)', js))
missing = sorted(i for i in ids if f'id="{i}"' not in html and i not in dynamic)
assert not missing, f"ids in app.js die niet in index.html staan: {missing}"
print("OK  UI statisch: app.js parseert, start-up-functies bestaan, alle $('#id')-elementen staan in index.html")

# WAN emulator settings: always on with FortiGate-VMs (no on/off switch), prefilled from the defaults
sec = html[html.index('id="wanemu-section"') - 200:]
sec = sec[:sec.index("</div><!-- /wanemu -->")]
assert 'class="only-fgt"' in sec[:300], "WAN-emulatie hoort bij de FortiGate-VM's (alleen bij Day-0 FortiGates)"
for i in ("wan-vmid", "wan-storage", "wan-bridge", "wan-uplink-vlan", "wan-uplink-ip", "wan-uplink-gw",
          "wan-seg-hub-block", "wan-seg-hub-vlanbase", "wan-seg-a-block", "wan-seg-a-vlanbase",
          "wan-seg-b-block", "wan-seg-b-vlanbase"):
    assert f'id="{i}"' in sec, i
# one VLAN + /24 per WAN: no shared subnet/gateway/reservation fields any more, no hub octet field
for gone in ("wan-seg-hub-subnet", "wan-seg-a-gw", "wan-seg-a-res", "wan-seg-b-vlan\""):
    assert gone not in html, gone
assert "wan1_oct" not in js and "data-hubpre" not in js and "data-hubaddr" in js
assert re.search(r"function\s+wanLinks\b", js), "JS mirror of model.wan_links"
topo = js[js.index("function topoSvg("):js.index("function portRoles(")]
assert "VLAN ${" in topo and "wanLinks(" in topo and "block" in topo, "diagram: block + VLAN range per ISP, address + VLAN per WAN"
assert "wanLinks(" in js[js.index("function portRoles("):js.index("function portsFixed(")]
assert 'type="checkbox"' not in sec, "geen aan/uit-schakelaar: de WAN-emulator is altijd aan"
assert "wanFill(" in js and "wanCollect(" in js and "t.wanemu = wanCollect()" in js
print("OK  UI: instellingen WAN-emulatie (container, uplink, ISP-HUB/ISP-A/ISP-B) zonder aan/uit, gevuld uit de standaarden")

# WAN-emulatie page: own view, cards per link in three segment columns, presets, error box
assert 'href="#/wanemu" data-nav="wanemu"' in html and 'id="v_wanemu"' in html
for i in ("wanemu-links", "wanemu-presets", "wanemu-err", "wanemu-addr"):
    assert f'id="{i}"' in html, i
assert '"wanemu"' in js[js.index("const VIEWS"):js.index("const VIEWS") + 120]
for f in ("wanLoad", "wanCard", "wanApply"):
    assert re.search(rf"(async\s+)?function\s+{f}\b", js), f
assert "/api/wanemu/links" in js and "/api/wanemu/preset/" in js and "/api/wanemu/reset" in js
print("OK  UI: pagina WAN-emulatie (kaart per link per segment, presets, foutmelding, verversen)")

# FortiFlex page + deploy page integration
assert 'href="#/fortiflex" data-nav="fortiflex"' in html and 'id="v_fortiflex"' in html
for i in ("ff_folder", "ff_user", "ff_pw", "ff_test", "ff_res", "ff_program", "ff_fmg", "ff_fgt", "ff_params", "ff_enabled", "ff_save",
          "ff_list", "ff_err", "btn_flexcheck", "flex_check"):
    assert f'id="{i}"' in html, i
assert 'type="password"' in html[html.index('id="ff_pw"') - 120:html.index('id="ff_pw"') + 40]
assert '"fortiflex"' in js[js.index("const VIEWS"):js.index("const VIEWS") + 140]
for f in ("ffLoad", "ffSave", "ffConfigs", "ffList", "flexApply"):
    assert re.search(rf"(async\s+)?function\s+{f}\b", js), f
assert 'list="ff_folders"' in html and 'id="ff_folders"' in html and "ffFolders" in js
for u in ("/api/fortiflex/folders", "/api/fortiflex/test", "/api/fortiflex/configs", "/api/fortiflex/entitlements", "/api/fortiflex/stop/", "/api/fortiflex/check"):
    assert u in js, u
print("OK  UI: pagina FortiFlex (credentials, testen, programma/configs, aan/uit, licenties + stop) en 'Licenties controleren'")

# deploy with FortiFlex: the wait is explained (lookup first, per-device plan, elapsed seconds)
assert 'id="deploy_status"' in html
for f in ("deployStatus", "flexPlanTable"):
    assert re.search(rf"function\s+{f}\b", js), f
dep = js[js.index('$("#btn_deploy").onclick'):]
dep = dep[:dep.index("\n};") + 3]
assert "/api/fortiflex/check" in dep and "deployStatus(msg(), plan)" in dep, "uitrolknop toont het licentieplan"
ds = js[js.index("function deployStatus"):]
assert "flexPlanTable(plan)" in ds[:ds.index("\n}")], "deployStatus tekent het plan per device"
assert dep.index("/api/fortiflex/check") < dep.index('post("/api/deploy"'), "eerst opzoeken (zichtbaar), dan de uitrol"
print("OK  UI: uitrol met FortiFlex toont eerst het licentieplan per device en een teller tijdens het ophalen")

# remote access: several terminal sessions (SSH + Proxmox console) in tabs, minimizable to a bar at the bottom
for i in ("wanemu-fgts", "sess_panel", "sess_tabs", "sess_min", "sess_bar", "term_user", "term_pw", "term_go", "term_box", "term_close"):
    assert f'id="{i}"' in html, i
assert 'type="password"' in html[html.index('id="term_pw"') - 80:html.index('id="term_pw"') + 40]
for f in ("wanFwdLoad", "sessOpen", "sessStart", "sessShow", "sessMin", "sessClose"):
    assert re.search(rf"(async\s+)?function\s+{f}\b", js), f
assert "/api/access" in js and "/api/access/password" in js and "/api/term/" in js and "/api/console/" in js
assert "data-console=" in js and "data-ssh=" in js and "j.clients" in js
assert html.index('id="wanemu-fgts"') < html.index('id="wanemu-presets"'), "remote access comes first"
assert re.search(r"cdn\.jsdelivr\.net/npm/xterm@\d+\.\d+\.\d+/", js) and re.search(r"xterm-addon-fit@\d+\.\d+\.\d+/", js)
assert "term_pw\").value = \"\"" in js.replace("'", '"'), "wachtwoordveld wordt na het verbinden leeggemaakt"
assert 'id="sess_bar"' in html and ".sess-bar" in open(os.path.join(root, "static", "style.css")).read()
# sessions stay reachable on every page: panel and bar outside the page sections
for i in ("sess_panel", "sess_bar"):
    pos = html.index(f'id="{i}"')
    assert html.rfind("<section", 0, pos) < html.rfind("</section>", 0, pos), f"{i} is inside a page section"
assert "needsForm" in js, "the password form belongs to its own session"
assert "SESS.get(s.key) !== s" in js, "a session closed while xterm loads opens nothing"
assert re.search(r"if \(old && old\.closed\)", js), "an ended session is reopened, not shown dead"
# console user per Proxmox server (VM consoles), password field + hint that it is stored encrypted
for i in ("srv_console_user", "srv_console_pw"):
    assert f'id="{i}"' in html, i
assert 'type="password"' in html[html.index('id="srv_console_pw"') - 80:html.index('id="srv_console_pw"') + 60]
assert "encrypted" in html[html.index('id="srv_console_user"') - 400:html.index('id="srv_console_pw"') + 800]
# console fields on their own row (own grid), the explanation below it, not inside a field (keeps the rows even)
grid = html[html.rindex('<div class="fields">', 0, html.index('id="srv_console_user"')):html.index('id="srv_console_pw"')]
assert 'id="srv_secret"' not in grid and '<div class="hint">' not in grid, "console velden in een eigen rij, zonder hint in het veld"
assert "console_user:" in js and "console_password:" in js
print("OK  UI: meerdere sessies (SSH + console) in tabs, minimaliseren, Console-knoppen, console-gebruiker per server")

# WAN-emulatie: releasing a slider / toggling "link down" applies at once (no hidden "Toepassen" step), with feedback
ch = js[js.index('$("#v_wanemu").addEventListener("change"'):]
ch = ch[:ch.index("\n});") + 4]
assert "wanApply(" in ch and "[data-wf]" in ch, "loslaten van een schuifje past de link direct toe"
assert "wan-ok" in js and ".wan-ok" in open(os.path.join(root, "static", "style.css")).read()
print("OK  UI: WAN-emulatie past een schuifje/link down direct toe bij loslaten, met bevestiging in de kaart")

assert "data-wps" not in js, "geen preset-keuze per link meer (alleen de storing-presets bovenaan)"
print("OK  UI: alleen storing-presets")

# topology picture on the deploy page, drawn from the form and redrawn on changes / scope switches
assert 'id="topo"' in html
for f in ("topoSvg", "topoDraw"):
    assert re.search(rf"function\s+{f}\b", js), f
assert "topoDraw()" in js[js.index("function draw()"):js.index("function draw()") + 1500], "draw() tekent de topologie"
assert "topoDraw()" in js[js.index("function scopeApply()"):js.index("function scopeApply()") + 600], "scope-wissel tekent opnieuw"
try:                                                       # render it for real with node (optional, like the syntax check)
    probe = ("const esc=s=>String(s);" + js[js.index("function linkNet("):js.index("function segPerDraw(")] +
             js[js.index("function topoSvg"):js.index("function topoDraw")] +
             "const T=" + __import__("json").dumps(__import__("app.model", fromlist=["x"]).merged({})) + ";"
             "const s=topoSvg(T,'all');if(!s.startsWith('<svg'))throw 'geen svg';"
             "for(const w of ['site1-H1','site1-3','ISP-A','WAN2 100.66.3.2','VLAN 3233','WAN1 100.64.1.2','gw 100.64.1.1','100.65.0.0/16',"
             "'VLAN 3211–3213','VLAN 3305','client-site1-1','wan-emu','FortiManager'])"
             "if(!s.includes(w))throw 'mist '+w;"
             "const d=topoSvg(T,'tpl');if(!d.includes('opacity'))throw 'tpl: niets gedimd';console.log('ok')")
    r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[:400]
except FileNotFoundError:
    pass
print("OK  UI: topologieplaatje (FMG, wan-emu, ISP-HUB/A/B, hubs, spokes, LAN-VLAN's, clients), per scope gedimd")

# no preview card and no dry-run switch: Deploy is always real; "Check licences" sits next to Deploy
for gone in ('id="btn_render"', 'id="btn_d0"', 'id="d0_dev"', 'id="dry"', "Preview &amp; deploy"):
    assert gone not in html, gone
for gone in ("btn_render", "btn_d0", "d0Opts", '$("#dry")'):
    assert gone not in js, gone
act = html[html.index('id="btn_deploy"') - 400:html.index('id="btn_deploy"')]
assert 'id="btn_flexcheck"' in act, "Check licences staat naast Deploy"
assert "dry_run: false" in js[js.index('$("#btn_deploy").onclick'):][:2500]
print("OK  UI: geen preview-blok en geen dry-run; Check licences naast Deploy")

assert 'id="wan-template"' not in html and "wan-template" not in js, "het CT-template kiest de gebruiker niet"
print("OK  UI: geen template-veld bij de WAN-emulatie")

# scope "LAN clients only": radio, hint, hidden FortiManager card, deploy without FMG/FGT methods, topology
assert 'id="scope_lan" value="lan"' in html and "lan:" in js[js.index("const SCOPE_HINT"):js.index("const SCOPE_HINT") + 3000]
assert 'class="card not-lan"' in html and '[data-scope="lan"] .not-lan' in open(os.path.join(root, "static", "style.css")).read()
dep = js[js.index('$("#btn_deploy").onclick'):][:1500]
assert '["fgt", "tpl", "dev", "lan"]' in dep and '["fmg", "tpl", "dev", "lan"]' in dep and 'sc === "lan"' in dep
try:
    probe = ("const esc=s=>String(s);" + js[js.index("function linkNet("):js.index("function segPerDraw(")] +
             js[js.index("function topoSvg"):js.index("function topoDraw")] +
             "const T=" + __import__("json").dumps(__import__("app.model", fromlist=["x"]).merged({})) + ";"
             "const s=topoSvg(T,'lan');const i=s.indexOf('client-site1-1');const g=s.lastIndexOf('<g',i);"
             "if(s.slice(g,i).includes('opacity'))throw 'client gedimd bij lan';console.log('ok')")
    r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[:400]
except FileNotFoundError:
    pass
print("OK  UI: keuze 'LAN clients only' (FortiManager-kaart weg, geen FMG/FGT, clients niet gedimd in de topologie)")

# Cleanup page: server/node, show what will be deleted, type DELETE, optional licence stop
assert 'href="#/cleanup" data-nav="cleanup"' in html and 'id="v_cleanup"' in html
for i in ("cl_server", "cl_node", "cl_show", "cl_list", "cl_confirm", "cl_licences", "cl_run", "cl_res", "cl_err"):
    assert f'id="{i}"' in html, i
assert '"cleanup"' in js[js.index("const VIEWS"):js.index("const VIEWS") + 160]
for f in ("clLoad", "clShow", "clRun"):
    assert re.search(rf"(async\s+)?function\s+{f}\b", js), f
assert "/api/cleanup/plan" in js and '"/api/cleanup"' in js and '"DELETE"' in js
print("OK  UI: pagina Cleanup (overzicht eerst, verwijderen alleen na het typen van DELETE)")

# own LAN client password (card visible with clients); 03 Topology hidden for "LAN clients only"
assert 'id="client_pw"' in html and 'type="password"' in html[html.index('id="client_pw"') - 120:html.index('id="client_pw"') + 60]
assert re.search(r'<div class="card[^"]*\bonly-clients\b[^"]*" id="sec_clients"', html), "kaart LAN clients"
assert re.search(r'<div class="card[^"]*\bnot-lan\b[^"]*" id="sec_devices"', html), "03 Topology weg bij LAN clients only"
assert "client_pw:" in js and "only-lan" not in html
css = open(os.path.join(root, "static", "style.css")).read()
assert '[data-scope="fmg"] .only-clients' in css and '[data-scope="tpl"] .only-clients' in css and '[data-scope="dev"] .only-clients' in css
print("OK  UI: eigen wachtwoord voor de LAN-clients; Topology-kaart weg bij LAN clients only")

# dark mode: toggle in the nav, remembered per browser, follows the system when not chosen, no flash on load
css = open(os.path.join(root, "static", "style.css")).read()
assert 'id="theme_btn"' in html and html.index('id="theme_btn"') < html.index("</header>")
head = html[:html.index("</head>")]
assert "localStorage" in head and "prefers-color-scheme: dark" in head and "data-theme" in head, "thema vóór het tekenen gezet"
dark = css[css.index('[data-theme="dark"]'):]
for v in ("--bg:", "--card:", "--text:", "--line:", "--topbar:"):
    assert v in dark[:800], v
assert ".topbar { background: var(--topbar)" in css
assert re.search(r"function\s+themeSet\b", js) and "fgt-sdwan-theme" in js
print("OK  UI: dark mode (knop in de navigatie, onthouden, volgt anders het systeem)")

# alignment: every field grid keeps the same column width (auto-fill, no stretching of short rows), and hub and
# branch cards have their fields in the same order (name, serial, FortiFlex token, loopback, WAN..., LAN...)
css = open(os.path.join(root, "static", "style.css")).read()
rule = re.search(r"\.fields \{[^}]*\}", css).group(0)
assert "auto-fill" in rule and "auto-fit" not in rule, rule
devrule = re.search(r"\.dev \.fields \{[^}]*\}", css).group(0)
assert "auto-fill" in devrule and "auto-fit" not in devrule, devrule
body = js[js.index("function dev(kind, d, i)"):js.index("function linkNet(")]
order = [body.index(x) for x in ('"Name"', '"Serial number"', "FortiFlex token", '"Loopback (BGP, router-id)"', '"wan1_intf"', 'LAN${j + 1} port')]
assert order == sorted(order), order
assert "lan-first" in body and re.search(r"\.dev \.fields > \.lan-first \{[^}]*grid-column-start: 1", css), "LAN velden op een eigen regel"
print("OK  UI: velden lijnen uit over alle kaarten (auto-fill); hub- en branchkaarten in dezelfde volgorde, LAN op een eigen regel")

# self-update instead of About; Simple (default) / Advanced deploy page; one password; Traffic-Gen always on
assert 'data-nav="about"' not in html and 'id="v_about"' not in html and '"about"' not in js[js.index("const VIEWS"):js.index("const VIEWS") + 200]
for i in ("upd_badge", "upd_panel", "upd_go", "mode_simple", "mode_adv", "pw_all", "mode_note"):
    assert f'id="{i}"' in html, i
assert "/api/version" in js and "/api/update" in js
assert 'data-eye="pw_all"' in html and "pw_all" in js[js.index("const PW_STRONG"):js.index("const PW_STRONG") + 300]
assert "tg_on" not in html and "tg_on" not in js
css = open(os.path.join(root, "static", "style.css")).read()
assert re.search(r'\[data-mode="simple"\] \.adv\s*\{[^}]*display:\s*none', css)
assert "fgt-sdwan-mode" in js
from html.parser import HTMLParser  # noqa: E402


class Anc(HTMLParser):                          # classes of every real ancestor (and the element) per id
    VOID = {"input", "br", "img", "meta", "link", "hr", "source", "option"}

    def __init__(self):
        super().__init__()
        self.stack, self.cls = [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        mine = set((a.get("class") or "").split())
        here = set().union(*[c for _, c in self.stack]) | mine
        if a.get("id"):
            self.cls[a["id"]] = here
        if tag not in self.VOID:
            self.stack.append((tag, mine))

    def handle_endtag(self, tag):
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k][0] == tag:
                del self.stack[k:]
                break


P = Anc()
P.feed(html)
adv = lambda i: "adv" in P.cls[i]
for i in ("hubs", "edges", "vrfs", "ports", "wan-segs", "pve_first", "pve_prefix", "fmg_cores", "fgt_cores",
          "fmg_pass", "fgt_newpw", "client_pw", "tg_pw", "wan-vmid", "hub2hub", "adom", "fmg_user"):
    assert adv(i), f"{i} hoort alleen in Advanced"
for i in ("pve_server", "pve_node", "pve_dstore", "pve_br", "wan-bridge", "wan-uplink-ip", "wan-uplink-vlan", "wan-uplink-gw",
          "fm_name", "fm_ip", "fm_gw", "r_multi", "pw_all", "img_fgt", "img_fmg", "fm_token", "tg_ip", "fmg_vlan", "fmg_br"):
    assert not adv(i), f"{i} hoort ook in Simple"
assert "custom topology" in html.lower() or "custom topology" in js.lower()
print("OK  UI: About weg, update-melding/-knop; Simple (standaard) / Advanced; één wachtwoord; Traffic-Gen altijd mee")

# review fixes for Simple: password card also for LAN clients only; empty fields filled from a shared password;
# errors in Simple switch to Advanced (hidden fields); existing FortiManager password stays separate and visible;
# a failed update is reported
assert 'id="sec_pw"' in html and "not-lan" not in html[html.rindex("<div", 0, html.index('id="sec_pw"')):html.index('id="sec_pw"')]
body = js[js.index("function pwAllFrom()"):js.index("function pwAllFrom()") + 800]
assert "if (same)" in body and ".value = vals[0]" in body, "lege velden krijgen het gedeelde wachtwoord"
se = js[js.index("function showErr("):js.index("function showErr(") + 700]
assert 'modeSet("advanced")' in se and "Advanced" in se
assert "PW_EXISTING" in js and 'only-existing-simple' in html
assert re.search(r'\[data-scope="(fgt|tpl|dev)"\][^{]*\.only-existing-simple', open(os.path.join(root, "static", "style.css")).read())
assert "last_update" in js and "journalctl -u fgt-sdwan-update" in js
print("OK  UI: review-fixes Simple (LAN-only wachtwoord, gedeeld wachtwoord, fouten -> Advanced, bestaande FMG apart, mislukte update)")


# FortiManager VLAN/bridge in the FortiManager card; own card "WAN emulation and Traffic Generator" with a choosable
# Traffic-Gen IP (Simple too)
def card_of(i):
    pos = html.index(f'id="{i}"')
    return html.rindex('<div class="card', 0, pos)
fm = card_of("fm_ip")
assert card_of("fmg_vlan") == fm and card_of("fmg_br") == fm, "FortiManager VLAN/bridge horen bij de FortiManager-kaart"
assert 'id="sec_wan"' in html and "WAN emulation and Traffic Generator" in html
w = card_of("wan-uplink-ip")
assert html[w:w + 80].count('id="sec_wan"') == 1 and card_of("wan-bridge") == w and card_of("tg_ip") == w and card_of("wan-segs") == w
assert card_of("pve_node") != w and card_of("img_fmg") == card_of("pve_node")
print("OK  UI: FortiManager-VLAN bij FortiManager; eigen kaart WAN emulation and Traffic Generator met kiesbaar Traffic-Gen-IP")

# Licenses page: FortiFlex or licence files; files per device; FortiFlex fields hidden in files mode
assert re.search(r'data-nav="fortiflex">Licenses<', html) and "<h1>Licenses</h1>" in html
for i in ("lic_mode_flex", "lic_mode_files", "lic_files", "lic_file"):
    assert f'id="{i}"' in html, i
assert "/api/licenses" in js and "?kind=" in js and "licLoad" in js
P2 = Anc(); P2.feed(html)
for i in ("ff_user", "ff_program", "ff_list"):
    assert "ff-only" in P2.cls[i], i
assert "files-only" in P2.cls["lic_files"] and "flex-only" in P2.cls["fm_token"]
assert "flex-only" in js[js.index("function dev(kind, d, i)"):js.index("function linkNet(")]       # per-device token
css = open(os.path.join(root, "static", "style.css")).read()
assert re.search(r'\[data-lic="files"\] \.(ff|flex)-only', css) and "files-only" in css
print("OK  UI: Licenses-pagina (FortiFlex of licentiebestanden, upload per device), FortiFlex-velden weg in bestandsmodus")
ld = js[js.index("function licDevices()"):js.index("function licDevices()") + 400]
assert "collect()" in ld, "Licenses-pagina volgt hernoemde devices"
ff = js[js.index("function flexFields()"):js.index("function flexFields()") + 900]
assert '["all", "fgt"].includes(sc)' in ff[ff.index('dataset.lic === "files"'):], "serials alleen vergrendeld waar ze uit de bestanden komen"
print("OK  UI: licentiepagina volgt de namen; serienummers bij Model devices only blijven invulbaar")

# the uplink comes from this server's last deployment (no fixed default in the code)
al = js[js.index("async function applyLast("):js.index("async function applyLast(") + 900]
assert '$("#wan-uplink-ip").value = up.ip' in al, al
assert '"uplink": {"bridge": "", "vlan": None, "ip": "", "gw": ""}' in open(os.path.join(root, "app", "model.py")).read()
# gateway follows the IP: first address of the subnet, unless the user typed a gateway of their own
assert "function gwFor(" in js and "gwAuto(\"#fm_ip\", \"#fm_gw\")" in js and "gwAuto(\"#wan-uplink-ip\", \"#wan-uplink-gw\")" in js
if shutil.which("node"):
    src = js[js.index("function gwFor("):]
    src = src[:src.index("\n}\n") + 3]
    probe = src + r"""
const t = [["10.10.106.150/24", "10.10.106.1"], ["192.168.1.250/24", "192.168.1.1"], ["10.20.3.77/22", "10.20.0.1"],
           ["10.10.106.1/24", ""], ["10.0.0.5", ""], ["10.0.0.5/31", ""], ["bla/24", ""], ["10.0.0.300/24", ""]];
const bad = t.filter(([i, g]) => gwFor(i) !== g).map(([i, g]) => `${i}: ${gwFor(i)} != ${g}`);
if (bad.length) { console.log(bad.join("; ")); process.exit(1); }
"""
    r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
print("OK  UI: uplink alleen uit de vorige uitrol (geen vaste default); gateway volgt het IP (eerste adres van het subnet)")

# IP/prefix fields: the address in the field, the prefix in its own dropdown (default /24); .value stays a.b.c.d/nn
assert "function splitCidr(" in js and "function prefixFields(" in js and '"wan-uplink-ip": "cidr"' in js
mk = js[js.index("function markIpFields()"):js.index("function markIpFields()") + 900]
assert "prefixFields()" in mk, "markIpFields voegt de dropdowns toe (ook na een nieuwe draw)"
assert ".pfx" in css, "dropdown naast het IP-veld"
if shutil.which("node"):
    src = js[js.index("function splitCidr("):]
    src = js[js.index("const OCT ="):js.index("function ipProblem(")] + src[:src.index("\n}\n") + 3]
    probe = src + r"""
const t = [["10.10.106.150/24", ["10.10.106.150", 24]], ["10.0.0.5/255.255.255.0", ["10.0.0.5", 24]], ["10.0.0.5", ["10.0.0.5", null]],
           ["", ["", null]], ["10.20.3.77/22", ["10.20.3.77", 22]], ["10.0.0.5/40", ["10.0.0.5/40", null]]];
const bad = t.filter(([i, g]) => JSON.stringify(splitCidr(i)) !== JSON.stringify(g)).map(([i, g]) => `${i}: ${JSON.stringify(splitCidr(i))}`);
if (bad.length) { console.log(bad.join("; ")); process.exit(1); }
"""
    r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
print("OK  UI: IP-velden met een aparte prefix-dropdown (default /24)")

# same order as the FortiManager (IP, gateway, bridge, VLAN): the uplink first, then the WAN emulator bridge
pos = [html.index(f'id="{i}"') for i in ("fm_ip", "fm_gw", "fmg_br", "fmg_vlan")]
assert pos == sorted(pos)
pos = [html.index(f'id="{i}"') for i in ("wan-uplink-ip", "wan-uplink-gw", "wan-uplink-bridge", "wan-uplink-vlan", "wan-bridge", "wan-vmid")]
assert pos == sorted(pos), "uplink: IP, gateway, bridge, VLAN (as the FortiManager), then WAN emulator bridge"
print("OK  UI: uplink-velden in dezelfde volgorde als de FortiManager")

# the update badge also appears on a page that stays open, and the browser does not cache the version check
assert "setInterval(updCheck" in js and 'fetch("/api/version", { cache: "no-store" })' in js
print("OK  UI: update-badge ook zonder refresh")

# Remote access: grouped (FortiManager / FortiGates / LAN clients), aligned columns, click the IP (or the SSH command) to copy it
wf = js[js.index("async function wanFwdLoad()"):js.index("async function copyText(")]
for g in ("FortiManager", "FortiGates", "LAN clients"):
    assert f">{g}<" in wf or f'"{g}"' in wf or f'"{g}s"' in wf, g
assert 'data-copy="${esc(d.ip)}"' in wf and 'data-copy="${esc(d.ssh)}"' in wf, "IP en SSH-commando klikbaar"
ck = js[js.index('$("#wanemu-fgts").addEventListener("click"'):]
assert "[data-copy]" in ck[:900] and "copyText(" in ck[:900]
assert ".acc-row" in css and "grid-template-columns" in css[css.index(".acc-row"):css.index(".acc-row") + 300]
print("OK  UI: Remote access gegroepeerd in kolommen; klik op IP/SSH-commando = kopiëren")
