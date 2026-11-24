"""Static UI checks (no browser): app.js parses, the start-up line only calls functions that exist, and every
element id used via $("#id") exists in index.html.  python -m tests.test_ui_static"""
import os, re, subprocess, sys
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
          "wan-seg-hub-vlan", "wan-seg-hub-subnet", "wan-seg-hub-gw", "wan-seg-a-vlan", "wan-seg-a-subnet", "wan-seg-a-gw",
          "wan-seg-a-res", "wan-seg-b-vlan", "wan-seg-b-subnet", "wan-seg-b-gw", "wan-seg-b-res"):
    assert f'id="{i}"' in sec, i
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
# console user per Proxmox server (VM consoles), password field + hint that it is stored encrypted
for i in ("srv_console_user", "srv_console_pw"):
    assert f'id="{i}"' in html, i
assert 'type="password"' in html[html.index('id="srv_console_pw"') - 80:html.index('id="srv_console_pw"') + 60]
assert "encrypted" in html[html.index('id="srv_console_user"') - 400:html.index('id="srv_console_pw"') + 600]
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
    probe = ("const esc=s=>String(s);" + js[js.index("function topoSvg"):js.index("function topoDraw")] +
             "const T=" + __import__("json").dumps(__import__("app.model", fromlist=["x"]).merged({})) + ";"
             "const s=topoSvg(T,'all');if(!s.startsWith('<svg'))throw 'geen svg';"
             "for(const w of ['site1-H1','site1-3','ISP-A','100.127.201.13','VLAN 3305','client-site1-1','wan-emu','FortiManager'])"
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
    probe = ("const esc=s=>String(s);" + js[js.index("function topoSvg"):js.index("function topoDraw")] +
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
