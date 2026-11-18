"""Full pipeline incl. ZTP bootstrap: mock FMG JSON-RPC + fake FortiOS SSH CLIs (1 FMG + 5 FGT).
Run: python tests/test_pipeline_ztp.py   (no running app needed)"""
import os
import sys
import threading
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import model
from app.bootstrap import Masker, Timing, ssh_connect
from app.fmg import Fmg, pipeline
from app import fmgtemplates
from tests import mock_fmg
from tests.fake_fortios import FakeDevice, State

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))

topo = model.merged({})
topo["dns_primary"] = "1.1.1.1"
topo["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1", initial_ip="192.168.1.98")
topo["hubs"][0]["wan1_gw"] = "100.64.1.6"
topo["hubs"][1]["wan1_gw"] = "100.64.2.6"
devs = topo["hubs"] + topo["edges"]
for i, d in enumerate(devs):
    d["serial"] = f"FGVMMLTM2600{i:04d}"
    d["initial_ip"] = f"192.168.1.{10 + i}"
assert model.validate(topo) == [], model.validate(topo)
project, inv = model.build(topo)
cfg = fmgtemplates.build_plan(topo, ORCH)

# fake devices: address -> device
fakes = {"192.168.1.98": FakeDevice(State("FMG-VM0000000001"))}
for i, d in enumerate(devs):
    fakes[d["initial_ip"]] = FakeDevice(State(d["serial"]))
    if d["wan1_mode"] == "static":     # same box answers on its new wan1 address after the IP move
        fakes[model.split_cidr(d["wan1_ip"])[0]] = fakes[d["initial_ip"]]
fakes["10.0.0.5"] = fakes["192.168.1.98"]

srv = HTTPServer(("127.0.0.1", 8098), mock_fmg.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

TOK_FMG, TOKS = "FMGTOKEN0123456789", {d["name"]: f"FGTTOKEN{i}ABCDEFGHIJ" for i, d in enumerate(devs)}
NEWPW = "Sup3r-Secret-Pw"
mask = Masker(TOK_FMG, NEWPW, *TOKS.values())
events = []


def emit(step, status, msg):
    events.append((step, status, mask(msg)))


def connector(host, user, pw):
    return ssh_connect("127.0.0.1", user, pw, port=fakes[host].port, mask=mask)


boot = {"dry": False, "fmg": True, "fgt": True, "fmg_token": TOK_FMG, "fgt_tokens": TOKS,
        "fmg_user": "admin", "fgt_user": "admin", "fmg_initial_pw": "", "fgt_initial_pw": "",
        "fmg_new_pw": NEWPW, "fgt_new_pw": NEWPW, "connector": connector,
        "waiter": lambda h, p, up, t: True, "timing": Timing(cmd=10, login=10, reboot_down=1, reboot_up=5, link_up=5),
        "online_timeout": 10}
fmg = Fmg("http://127.0.0.1:8098", "admin", NEWPW)
pipeline(topo, cfg, fmg, emit, boot)

by = {}
for s, st, m in events:
    by.setdefault(s, []).append(st)
print({k: v[-1] for k, v in by.items()})
assert by["pve_fmg"] == by["pve_fgt"] == ["skipped"], "SSH-methode mag geen Proxmox-VM's aanmaken"
assert all(v[-1] == "ok" for k, v in by.items() if k not in ("pve_fmg", "pve_fgt")), by
f = fakes["192.168.1.98"].state
assert f.licensed and f"execute vm-license {TOK_FMG}" in f.cmds and "set ip 10.0.0.5 255.255.255.0" in f.cmds
for i, d in enumerate(devs):
    st = fakes[d["initial_ip"]].state
    assert st.licensed and f"execute vm-license {TOKS[d['name']]}" in st.cmds, d["name"]
    assert ('set mode static' in st.cmds) == (d["wan1_mode"] == "static"), d["name"]
    assert 'set fmg "10.0.0.5"' in st.cmds
assert not any(TOK_FMG in m or NEWPW in m or any(t in m for t in TOKS.values()) for _, _, m in events)
order = [s for s, _, _ in events]
firsts = [order.index(s) for s in ("bootstrap_fmg", "preflight", "adom", "devices", "templates", "bootstrap_fgt", "install")]
assert firsts == sorted(firsts), firsts
m = __import__("requests").get("http://127.0.0.1:8098").json()
assert len(m["devices"]) == 5 and any(c[1].startswith("pm/tmplgrp") for c in m["calls"]) and any(c[1] == "securityconsole/install/package" for c in m["calls"])
print("OK  ZTP-pipeline: FMG + 5 FortiGates (2 static, 3 dhcp) gebootstrapt, gelicenseerd, geregistreerd,")
print("    templates+install erna; volgorde klopt; geen token/wachtwoord in events")

# dry-run: nothing touches the network, tokens masked
events.clear()
boot2 = dict(boot, dry=True, connector=lambda *a: (_ for _ in ()).throw(AssertionError("dry-run mag niet verbinden")))
pipeline(topo, cfg, Fmg("http://127.0.0.1:1", dry_run=True), emit, boot2)
assert any("[dry-run]" in m and "vm-license ********" in m for _, _, m in events)
print("OK  dry-run met bootstrap: alleen gelogd, token als ******** getoond")
