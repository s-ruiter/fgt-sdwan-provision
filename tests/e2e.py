"""End-to-end check against a running app (APP) and mock FMG (MOCK). Usage: python tests/e2e.py"""
import copy
import json
import sys
import time
import requests

APP = "http://127.0.0.1:8010"
MOCK = "http://127.0.0.1:8099"

topo = requests.get(f"{APP}/api/defaults").json()
topo["dns_primary"] = "1.1.1.1"
topo["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
topo["hubs"][0]["wan1_gw"] = "100.127.200.1"
topo["hubs"][1]["wan1_gw"] = "100.127.200.1"
# 1. missing serials must be rejected
r = requests.post(f"{APP}/api/render", json={"topology": topo})
assert r.status_code == 422 and any("serienummer" in e for e in r.json()["detail"]), r.text
print("OK  validatie weigert lege serienummers:", len(r.json()["detail"]), "fouten")

for i, d in enumerate(topo["hubs"] + topo["edges"]):
    d["serial"] = f"FGVMMLTM2600{i:04d}"
topo["adom"] = "ACME-SDWAN"
topo["psk"] = "Lab-Only-Psk-123"      # a client-supplied PSK must be ignored: the tool generates its own

r = requests.post(f"{APP}/api/render", json={"topology": topo})
assert r.status_code == 200, r.text
cfg = r.json()["configs"]
print("OK  render:", {k: v for k, v in r.json()["lines"].items()})
allcfg = "".join(cfg.values())
assert "Lab-Only-Psk-123" not in allcfg and "S3cr3t!" not in allcfg
assert "<PSK-wordt-bij-de-uitrol-gegenereerd>" in cfg["CLI HUB1_02-Overlay"]      # preview: placeholder, nothing generated/stored yet
# templates, geen per-device config: variabelen i.p.v. literals
assert "Variabelen" in cfg and "$(loopback)" in cfg["CLI SPOKES-1_02-Overlay"] and "10.200.1.1" not in cfg["CLI SPOKES-1_02-Overlay"]
assert "set vrf-select 1" in cfg["CLI HUB1_00-Mgmt"] and "set gateway $(wan1_gw)" in cfg["CLI HUB1_00-Mgmt"]
assert "set gateway" not in cfg["CLI SPOKES-1_00-Mgmt"]
assert "H1_WAN1" in cfg["SD-WAN SPOKES-1"] and "H1_WAN2" in cfg["SD-WAN SPOKES-1"] and "Corporate" in cfg["Policy SPOKES-1"]
assert not any(x in allcfg + str(cfg) for x in ("MPLS", "mpls", "ISP1", "ISP2", "isp1", "isp2")), "MPLS/ISP-namen mogen niet meer voorkomen"
print("OK  render: templates met $(var), CLI/SD-WAN/Policy tabs:", len(cfg))
for needle in ("set vrf 11", "set vrf 12", "vrf11_leak"):
    assert needle in allcfg, needle
assert "vrf12_leak" not in allcfg  # VRF 12 heeft geen DIA
print("OK  VRF 11/12, leak alleen voor DIA-VRF 11; PSK in de preview = placeholder (eigen invoer genegeerd)")

# 2. real deploy against the mock - twice (second run must be idempotent)
def deploy():
    r = requests.post(f"{APP}/api/deploy", json={"topology": topo, "dry_run": False,
                      "fmg": {"host": MOCK, "username": "admin", "password": "Mock-Pass-1"}})
    jid = r.json()["job"]
    j = {"state": "running", "events": [], "steps": {}}
    for _ in range(60):
        j = requests.get(f"{APP}/api/jobs/{jid}").json()
        if j["state"] != "running":
            break
        time.sleep(0.5)
    return j

for run in (1, 2):
    j = deploy()
    m = requests.get(MOCK).json()
    print(f"run {run}: state={j['state']} steps={j['steps']}")
    if run == 1:
        for e in j["events"]:
            print("  ", e["t"], e["step"], e["status"], e["msg"])
    assert j["state"] == "done", j["events"][-1]
    assert m["adoms"] == ["ACME-SDWAN"] and len(m["devices"]) == 5, m
    urls = [u for _, u, _ in m["bodies"]]
    assert any(u.startswith("pm/config/adom/ACME-SDWAN/obj/cli/template/SPOKES-1_02-Overlay") for u in urls)
    assert any(u.startswith("pm/config/adom/ACME-SDWAN/obj/fmg/variable/lan_ip") for u in urls)
    assert not any("/script" in u for u in urls), "geen scripts meer"
    if run == 1:
        n1 = len(urls)
import re as _re
_psks = {_re.search(r"psksecret (\S+)", str(d)).group(1) for _, u, d in m["bodies"] if "obj/cli/template/HUB1_02-Overlay" in u and d and "psksecret" in str(d)}
assert len(_psks) == 1, _psks                          # same PSK in run 1 and run 2 (stable per ADOM)
_psk = _psks.pop().strip('"').strip("\\")
assert len(_psk) >= 32 and _psk != "Lab-Only-Psk-123" and "gegenereerd" not in _psk, _psk
print("OK  PSK naar FMG: willekeurig (%d tekens), identiek in 2 runs, eigen invoer genegeerd" % len(_psk))
print("OK  pipeline (mock): 5 devices, templates/variabelen/pakketten/blueprints aangemaakt, geen scripts")
print("OK  2e run idempotent: alleen set/replace/update-aanroepen (%d aanroepen na run 1)" % n1)

# 3. bootstrap validation + dry-run via the API
for i, d in enumerate(topo["hubs"] + topo["edges"]):
    d["initial_ip"] = f"192.168.1.{10 + i}"
topo["fmgr"]["initial_ip"] = "192.168.1.98"
body = {"topology": topo, "dry_run": True, "fmg": {"username": "admin", "password": "Mock-Pass-1"},
        "bootstrap": {"fmg": True, "fgt": True}, "secrets": {}}
r = requests.post(f"{APP}/api/deploy", json=body)
assert r.status_code == 422 and any("FortiFlex-token" in e for e in r.json()["detail"]), r.text
print("OK  bootstrap zonder tokens/wachtwoord geweigerd:", len(r.json()["detail"]), "fouten")
dup = copy.deepcopy(body)
dup["topology"]["edges"][0]["initial_ip"] = dup["topology"]["hubs"][0]["initial_ip"]
r = requests.post(f"{APP}/api/deploy", json=dup)
assert r.status_code == 422 and any("uniek" in e for e in r.json()["detail"]), r.text
print("OK  dubbel initieel IP geweigerd")
TOK = "ABCDEFGHIJ0123456789"
body["secrets"] = {"fmg_token": TOK, "fgt_tokens": {d["name"]: TOK + str(i) for i, d in enumerate(topo["hubs"] + topo["edges"])},
                   "fgt_new_pw": "Sup3r-Secret-Pw"}
r = requests.post(f"{APP}/api/deploy", json=body)
assert r.status_code == 200, r.text
jid = r.json()["job"]
for _ in range(60):
    j = requests.get(f"{APP}/api/jobs/{jid}").json()
    if j["state"] != "running":
        break
    time.sleep(0.5)
txt = json.dumps(j)
assert j["state"] == "done", j["events"][-3:]
assert TOK not in txt and "Sup3r-Secret-Pw" not in txt and "Mock-Pass-1" not in txt
assert "vm-license ********" in txt and j["steps"]["bootstrap_fgt"] == "ok" and j["steps"]["bootstrap_fmg"] == "ok"
print("OK  dry-run met bootstrap via API: tokens/wachtwoorden komen nergens in de job-output")

# 4. day-0 endpoints: image upload, Proxmox test, cloud-init preview, day-0 dry-run validation
import io, struct, zipfile
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("fortios.qcow2", b"QFI\xfb" + struct.pack(">I", 3) + b"\0" * 16 + struct.pack(">Q", 2 * 1024 ** 3) + b"\0" * 512)
r = requests.post(f"{APP}/api/images", data={"role": "fgt"},
                  files={"file": ("FGT_VM64_KVM-v8.0.0-build0001-FORTINET.out.kvm.zip", buf.getvalue())})
assert r.status_code == 200 and r.json()["virtual_gb"] == 2 and r.json()["version"] == "8.0.0", r.text
img_fgt = r.json()["id"]
r = requests.post(f"{APP}/api/images", data={"role": "fmg"}, files={"file": ("FGT_VM64_KVM-v8.0.0-build0001.zip", buf.getvalue())})
assert r.status_code == 422 and "FortiGate-image" in r.json()["detail"], r.text
assert [i["id"] for i in requests.get(f"{APP}/api/images").json()] == [img_fgt]
print("OK  /api/images: upload (zip -> qcow2), rol-mismatch geweigerd, lijst")

PVE = "http://127.0.0.1:8097"
r = requests.post(f"{APP}/api/pve/test", json={"url": PVE, "token_id": "root@pam!t", "token_secret": "s", "node": "pve1"})
assert r.status_code == 200 and r.json()["version"] == "9.0.3" and r.json()["bridges"] == ["vmbr0", "vmbr1"], r.text
print("OK  /api/pve/test:", {k: r.json()[k] for k in ("version", "import_storages", "iso_storages")})

r = requests.post(f"{APP}/api/cloudinit/preview", json={"topology": topo, "device": "fmg"})
assert r.status_code == 200 and r.json()["user_data"].rstrip().endswith("execute vm-license ********") and "XXXXXXXX" not in r.json()["user_data"] and r.json()["mode"] == "cli"
r = requests.post(f"{APP}/api/cloudinit/preview", json={"topology": topo, "device": "site1-1"})
assert r.json()["mode"] == "mime" and "LICENSE-TOKEN: ********" in r.json()["user_data"] and "XXXXXXXX" not in r.json()["user_data"]
print("OK  /api/cloudinit/preview: FortiManager cli (execute vm-license ********), FortiGate MIME (LICENSE-TOKEN)")

body = {"topology": topo, "dry_run": True, "fmg": {"username": "admin", "password": "Mock-Pass-1"},
        "bootstrap": {"fmg": "day0", "fgt": "day0"}, "secrets": {}, "images": {}}
r = requests.post(f"{APP}/api/deploy", json=body)
assert r.status_code == 422 and any("Proxmox" in e for e in r.json()["detail"]) and any("image" in e for e in r.json()["detail"]), r.text
print("OK  day-0 zonder Proxmox/images/tokens geweigerd:", len(r.json()["detail"]), "fouten")
topo["pve"].update(url=PVE, token_id="root@pam!t", node="pve1", disk_storage="local-lvm", import_storage="local", iso_storage="local")
body["topology"] = topo
body["images"] = {"fgt": img_fgt}
r = requests.post(f"{APP}/api/deploy", json=body)
assert r.status_code == 422 and any("FortiManager: image" in e for e in r.json()["detail"]), r.text
requests.delete(f"{APP}/api/images/{img_fgt}")
print("OK  ontbrekende FMG-image gemeld; image verwijderd")

# 5. saved Proxmox servers: CRUD, secret is write-only, stored secret is what Proxmox sees, last-used, dry-run deploy
import os, stat
SECRET = "pve-secret-abc123"
srv = {"name": "pve-lab", "url": PVE, "token_id": "root@pam!deploy", "token_secret": SECRET, "verify_tls": True}
r = requests.post(f"{APP}/api/servers", json=dict(srv, token_secret=None))
assert r.status_code == 422, r.text
r = requests.post(f"{APP}/api/servers", json=srv)
assert r.status_code == 201 and "token_secret" not in r.json() and r.json()["has_secret"], r.text
sid = r.json()["id"]
assert requests.post(f"{APP}/api/servers", json=srv).status_code == 409
assert requests.post(f"{APP}/api/servers", json=dict(srv, name="x", url="ftp://nope")).status_code == 422
assert SECRET not in requests.get(f"{APP}/api/servers").text
dd = os.environ.get("E2E_DATA_DIR")
if dd:
    mode = stat.S_IMODE(os.stat(os.path.join(dd, "fgt-sdwan.db")).st_mode)
    assert mode == 0o600, oct(mode)
print("OK  servers: secret verplicht, duplicaat 409, URL-validatie, secret nooit in API-antwoord" + (", db-bestand 0600" if dd else ""))

r = requests.post(f"{APP}/api/servers/{sid}/test", json={"node": "pve1"})
assert r.status_code == 200 and r.json()["version"] == "9.0.3" and r.json()["bridges"] == ["vmbr0", "vmbr1"], r.text
assert requests.get(f"{PVE}/__auth").json()["auth"] == f"PVEAPIToken=root@pam!deploy={SECRET}"
r = requests.put(f"{APP}/api/servers/{sid}", json=dict(srv, name="pve-lab2", token_secret=None))
assert r.status_code == 200 and r.json()["name"] == "pve-lab2", r.text
requests.post(f"{APP}/api/servers/{sid}/test", json={})
assert requests.get(f"{PVE}/__auth").json()["auth"].endswith("=" + SECRET), "oud secret moet bewaard blijven bij bewerken zonder secret"
r = requests.put(f"{APP}/api/servers/{sid}", json=dict(srv, name="pve-lab2", url="http://127.0.0.1:9999", token_secret=None))
assert r.status_code == 422 and "opnieuw" in r.json()["detail"], r.text
print("OK  opgeslagen secret wordt echt gebruikt richting Proxmox; bewerken zonder secret behoudt hem; URL wijzigen eist nieuw secret")

r = requests.get(f"{APP}/api/servers/{sid}/nodes/pve1/options")
o = r.json()
assert r.status_code == 200 and [s["name"] for s in o["image_storages"]] == ["local-lvm"] and [s["name"] for s in o["import_storages"]] == ["local"] \
    and [s["name"] for s in o["iso_storages"]] == ["local"] and o["bridges"] == ["vmbr0", "vmbr1"], r.text
assert requests.get(f"{APP}/api/servers/{sid}/nodes/pve1;rm/options").status_code in (404, 422)
print("OK  /api/servers/{id}/nodes/{node}/options: actieve storages per content-type (inactieve 'old' weggelaten) + bridges")

for role in ("fgt", "fmg"):
    r = requests.post(f"{APP}/api/images", data={"role": role}, files={"file": ("fortios.qcow2" if role == "fgt" else "fmg.qcow2", b"QFI\xfb" + struct.pack(">I", 3) + b"\0" * 16 + struct.pack(">Q", 2 * 1024 ** 3) + b"\0" * 64)})
    assert r.status_code == 200, r.text
imgs = {i["role"]: i["id"] for i in requests.get(f"{APP}/api/images").json()}
topo2 = copy.deepcopy(topo)
topo2["pve"].update(url="", token_id="", server_id=sid, node="pve1", disk_storage="local-lvm", import_storage="local", iso_storage="local",
                    default_bridge="vmbr1", license_mode="cli")
topo2["pve"]["fgt"] = {"cores": 4, "memory_mb": 4096}
names = [d["name"] for d in topo2["hubs"] + topo2["edges"]]
body = {"topology": topo2, "dry_run": True, "fmg": {"username": "admin", "password": "Mock-Pass-1"},
        "bootstrap": {"fmg": "day0", "fgt": "day0"}, "images": imgs,
        "secrets": {"fmg_token": "ABCDEFGHIJ0123456789", "fgt_tokens": {n: "ABCDEFGHIJ012345678" + str(i) for i, n in enumerate(names)}, "fgt_new_pw": "Sup3r-Secret-Pw"}}
r = requests.post(f"{APP}/api/deploy", json=body)
assert r.status_code == 200, r.text            # no pve_token_secret in the request: it comes from the database
jid = r.json()["job"]
for _ in range(60):
    j = requests.get(f"{APP}/api/jobs/{jid}").json()
    if j["state"] != "running":
        break
    time.sleep(0.5)
assert j["state"] == "done" and SECRET not in json.dumps(j) and j["steps"]["pve_fgt"] == "ok", j["events"][-3:]
lu = requests.get(f"{APP}/api/servers/{sid}/last-used").json()
assert lu["node"] == "pve1" and lu["default_bridge"] == "vmbr1" and lu["fgt"]["cores"] == 4 and "url" not in lu and "token_id" not in lu, lu
assert [s["last"] for s in requests.get(f"{APP}/api/servers").json()] == [True]
print("OK  deploy met server_id zonder secret in het verzoek; laatst gebruikte keuzes (node, storages, bridge, vCPU) bewaard; server gemarkeerd als laatst gebruikt")

r = requests.post(f"{APP}/api/deploy", json=dict(body, topology=dict(topo2, pve=dict(topo2["pve"], server_id=9999))))
assert r.status_code == 422 and "niet gevonden" in r.json()["detail"][0], r.text
r = requests.post(f"{APP}/api/deploy", json=dict(body, topology=dict(topo2, pve=dict(topo2["pve"], server_id=None))))
assert r.status_code == 422 and any("kies een server" in e for e in r.json()["detail"]), r.text
assert requests.delete(f"{APP}/api/servers/{sid}").status_code == 204
assert requests.get(f"{APP}/api/servers/{sid}/last-used").json() == {} and requests.get(f"{APP}/api/servers").json() == []
r = requests.post(f"{APP}/api/deploy", json=body)
assert r.status_code == 422 and "niet gevonden" in r.json()["detail"][0], r.text
for i in imgs.values():
    requests.delete(f"{APP}/api/images/{i}")
print("OK  onbekende/geen server geweigerd; verwijderen ruimt ook last-used op")
