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
topo["hubs"][0]["wan1_gw"] = "100.64.1.6"
topo["hubs"][1]["wan1_gw"] = "100.64.2.6"
# 1. missing serials must be rejected
r = requests.post(f"{APP}/api/render", json={"topology": topo})
assert r.status_code == 422 and any("serienummer" in e for e in r.json()["detail"]), r.text
print("OK  validatie weigert lege serienummers:", len(r.json()["detail"]), "fouten")

for i, d in enumerate(topo["hubs"] + topo["edges"]):
    d["serial"] = f"FGVMMLTM2600{i:04d}"
topo["adom"] = "ACME-SDWAN"
topo["psk"] = "Lab-Only-Psk-123"

r = requests.post(f"{APP}/api/render", json={"topology": topo})
assert r.status_code == 200, r.text
cfg = r.json()["configs"]
print("OK  render:", {k: v for k, v in r.json()["lines"].items()})
h1 = cfg["site1-H1"]
assert "Lab-Only-Psk-123" in h1 and "S3cr3t!" not in h1
allcfg = "".join(cfg.values())
# ZTP/underlay: static hubs get set mode static + default route in VRF 1 + allowaccess kept; DHCP edges stay dhcp
assert "set mode static" in cfg["site1-H1"] and "set gateway 100.64.1.6" in cfg["site1-H1"]
assert "set vrf-select 1" in cfg["site1-H1"] and 'set fmg "10.0.0.5"' in cfg["site1-1"]
assert "set allowaccess ping https ssh fgfm" in cfg["site1-H1"] and "set allowaccess ping https ssh fgfm" in cfg["site1-1"]
assert "set mode dhcp" in cfg["site1-1"] and "set gateway" not in cfg["site1-1"].split("# ZTP underlay")[1].split("config system dns")[0]
print("OK  wan1 static (hub) / dhcp (branch), allowaccess behouden, FMG vrf-select in script")
for needle in ("set vrf 11", "set vrf 12", "vrf11_leak"):
    assert needle in allcfg, needle
assert "vrf12_leak" not in allcfg  # VRF 12 heeft geen DIA
print("OK  VRF 11/12, leak alleen voor DIA-VRF 11, eigen PSK aanwezig")

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
    assert m["adoms"] == ["ACME-SDWAN"] and len(m["devices"]) == 5 and len(m["scripts"]) == 5, m
    assert "execute batch" not in m["script_sample"]
print("OK  pipeline (mock): 5 devices, 5 scripts, batch-wrapper gestript")
print("OK  2e run idempotent: geen dubbele scripts, bestaande scripts bijgewerkt (set x%d)" % m["sets"])
assert m["sets"] == 5

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
