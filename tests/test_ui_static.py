"""Static UI checks (no browser): app.js parses, the start-up line only calls functions that exist, and every
element id used via $("#id") exists in index.html.  python -m tests.test_ui_static"""
import os, re, subprocess, sys
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
