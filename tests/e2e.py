"""End-to-end check against a running app (APP) and mock FMG (MOCK). Usage: python tests/e2e.py"""
import copy
import json
import sys
import time
import requests

APP = "http://127.0.0.1:8010"
MOCK = "http://127.0.0.1:8099"

topo = requests.get(f"{APP}/api/defaults").json()
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
for needle in ("set vrf 11", "set vrf 12", "vrf11_leak"):
    assert needle in allcfg, needle
assert "vrf12_leak" not in allcfg  # VRF 12 heeft geen DIA
print("OK  VRF 11/12, leak alleen voor DIA-VRF 11, eigen PSK aanwezig")

# 2. real deploy against the mock - twice (second run must be idempotent)
def deploy():
    r = requests.post(f"{APP}/api/deploy", json={"topology": topo, "dry_run": False,
                      "fmg": {"host": MOCK, "username": "admin", "password": "x"}})
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
