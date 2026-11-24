"""Deploy scope: everything, only the FortiManager, or only the FortiGates (fake Proxmox + mock FMG).
Run: ORCH_DIR=../sdwan-advpn-reference python tests/test_scope.py"""
import os
import sys
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from app import fmgtemplates, main, model
import labuplink  # noqa: E402,F401  (fills in the lab uplink: it has no default)
from app.fmg import Fmg, FmgError, pipeline
from tests import fake_proxmox, mock_fmg
from tests.test_day0_units import fake_qcow2, topo as base_topo

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))
PVE, FMG = "http://127.0.0.1:8095", "http://127.0.0.1:8096"
fake_proxmox.reset()
fake_proxmox.start(8095)
srv = HTTPServer(("127.0.0.1", 8096), mock_fmg.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

t = base_topo()
t["pve"].update(url=PVE, token_id="root@pam!deploy", node="pve1", disk_storage="local-lvm", import_storage="local",
                iso_storage="local", default_bridge="vmbr0")
devs = t["hubs"] + t["edges"]
cfg = fmgtemplates.build_plan(t, ORCH)
td = tempfile.mkdtemp()
fgt_img, fmg_img = Path(td, "fortios.qcow2"), Path(td, "fmg.qcow2")
fgt_img.write_bytes(fake_qcow2())
fmg_img.write_bytes(fake_qcow2())
PW = "Sup3r-Secret-Pw"
TOKS = {d["name"]: f"FGTTOKEN{i}ABCDEFGHIJ" for i, d in enumerate(devs)}


from tests import fake_wanemu  # noqa: E402
from app.wanemu.setup import new_ssh_key  # noqa: E402
WANEMU_KEY = new_ssh_key()


def boot(**kw):
    b = {"dry": False, "fmg": "day0", "fgt": "day0", "fmg_token": "FMGTOKEN0123456789", "fgt_tokens": TOKS,
         "fmg_new_pw": PW, "fgt_new_pw": PW, "client_pw": "Client-pw1", "pve_secret": "pve-secret", "images": {"fgt": str(fgt_img), "fmg": str(fmg_img)},
         "waiter": lambda h, p, up, tmo: True, "poll": 0, "login_delay": 0, "online_timeout": 5, "fgt_boot_timeout": 5}
    b.setdefault("wanemu_secrets", {"ssh_key": WANEMU_KEY, "agent_token": "agenttoken1234567890"})
    b.setdefault("wanemu_connector", fake_wanemu.connector(fake_wanemu.FakeShell(installed=True)))
    b.update(kw)
    return b


def run(**kw):
    ev = []
    pipeline(t, cfg, Fmg(FMG, "admin", PW), lambda s, st, m: ev.append((s, st, m)), boot(**kw))
    last = {}
    for s, st, _ in ev:
        if st != "info":
            last[s] = st
    return last, ev


names = lambda: {v["name"] for v in fake_proxmox.STATE["vms"].values()}

# ---------------------------------------------------------------- only the FortiManager: no serials needed
# (the hub wan1 gateway IS needed: it is in the hub SD-WAN template)
import copy
bare = copy.deepcopy(t)
for d in bare["hubs"] + bare["edges"]:
    d["serial"] = ""
assert any("serial number" in x for x in model.validate(bare))                       # still required for FortiGates
assert model.validate(bare, devices=False) == [], model.validate(bare, devices=False)
nogw = copy.deepcopy(bare)
nogw["hubs"][0]["wan1_gw"] = ""                    # filled in from ISP-HUB (wan-emu)
assert model.validate(nogw, devices=False) == [] and nogw["hubs"][0]["wan1_gw"] == "100.64.1.1"
bare_cfg = fmgtemplates.build_plan(bare, ORCH)
mock_fmg.STATE["bodies"].clear()
ev = []
pipeline(bare, bare_cfg, Fmg(FMG, "admin", PW), lambda s_, st, m: ev.append((s_, st, m)), boot(scope="fmg"))
last = {s_: st for s_, st, _ in ev if st != "info"}
assert names() == {"fmg"}, names()
skip = ("devices", "pve_wan", "pve_fgt", "bootstrap_fgt", "install", "clients")
assert {k: last[k] for k in skip} == dict.fromkeys(skip, "skipped"), last
assert all(last[k] == "ok" for k in ("pve_fmg", "bootstrap_fmg", "preflight", "fmg_settings", "adom", "templates")), last
assert any(s_ == "pve_fgt" and "FortiManager only" in m for s_, _, m in ev)
bodies = mock_fmg.STATE["bodies"]
assert not any(u == "dvm/cmd/add/device" for _, u, _ in bodies)                     # no model devices
assert not any(u.startswith("securityconsole/") for _, u, _ in bodies)               # no install
assert not any(u.endswith("/object member") for _, u, _ in bodies)                   # device groups stay empty
assert not any("dynamic_mapping" in str(d) or "_scope" in str(d) for _, _, d in bodies)   # nothing refers to a device
for kind in ("pkg/adom/", "tmplgrp/adom/", "wanprof/adom/", "obj/fmg/device/blueprint/", "obj/cli/template/"):
    assert any(kind in u for _, u, _ in bodies), kind                               # templates for hubs and spokes are there
print("OK  alleen FortiManager: zonder serienummers/wan1-gateways; FMG-VM, ADOM en templates, geen model devices/FortiGates/install")

# ---------------------------------------------------------------- only the FortiGates (FMG from the previous run)
last, ev = run(scope="fgt")
assert names() == {"fmg", *TOKS}, names()
assert fake_proxmox.STATE["creates"] == 1 + len(devs), fake_proxmox.STATE["creates"]       # FMG not created again
assert last["pve_fmg"] == last["bootstrap_fmg"] == last["fmg_settings"] == "skipped", last
assert any(s == "pve_fmg" and "FortiGates only" in m for s, _, m in ev)
assert all(last[k] == "ok" for k in ("preflight", "adom", "devices", "pve_wan", "pve_fgt", "bootstrap_fgt", "templates", "install")), last
assert fake_proxmox.STATE["cts"], "alleen FortiGates (Day-0): WAN-emulator hoort erbij"
assert any(s == "adom" and "reused" in m for s, _, m in ev)
assert any("dynamic_mapping" in str(d) for _, _, d in mock_fmg.STATE["bodies"])        # per-device values filled now
inst = [(u, d) for _, u, d in mock_fmg.STATE["bodies"] if u.startswith("securityconsole/")]
assert [d["scope"][0]["name"] for u, d in inst if u.endswith("install/device")] == [x["name"] for x in devs], inst
assert [u for u, _ in inst][:len(devs)] == ["securityconsole/install/device"] * len(devs)             # same as model devices
assert len([1 for u, _ in inst if u.endswith("install/package")]) == len(cfg["roles"])
o = [s_ for s_, _, _ in ev]
assert o.index("templates") < o.index("pve_fgt") < o.index("install")                    # templates before the VMs
print("OK  alleen FortiGates: geen FMG-VM, bestaande ADOM hergebruikt, FortiGate-VM's gemaakt, install uitgevoerd")

# ---------------------------------------------------------------- 'fgt' without a reachable FortiManager: clear error
try:
    pipeline(t, cfg, Fmg("http://127.0.0.1:1", "admin", PW), lambda *a: None, boot(scope="fgt", fgt="none"))
    raise AssertionError("moest falen")
except FmgError as e:
    assert "existing" in str(e) or "reachable FortiManager" in str(e), e
print("OK  alleen FortiGates zonder bereikbare FortiManager: duidelijke foutmelding")

# ---------------------------------------------------------------- only the templates, to an existing FortiManager
tb = copy.deepcopy(bare)
tb["fmgr"]["gateway"] = ""                                                         # existing FMG: IP is only the API address
assert any("gateway" in x for x in model.validate(tb, devices=False))
assert model.validate(tb, devices=False, fmg_vm=False) == [], model.validate(tb, devices=False, fmg_vm=False)
tb["fmgr"]["ip"] = "10.10.106.11"                                                    # existing FMG: plain address is fine
assert model.validate(tb, devices=False, fmg_vm=False) == [], model.validate(tb, devices=False, fmg_vm=False)
assert any("a.b.c.d/nn" in x for x in model.validate(tb, devices=False))             # a new FMG-VM still needs the prefix
assert model.split_cidr(tb["fmgr"]["ip"])[0] == "10.10.106.11"
fg = copy.deepcopy(t)
fg["fmgr"].update(gateway="", name="", port="")                                     # 'Alleen FortiGates': existing FMG
assert not any("FortiManager" in x for x in model.validate(fg, devices=True, fmg_vm=False)), model.validate(fg, True, False)
creates = fake_proxmox.STATE["creates"]
mock_fmg.STATE["bodies"].clear()
mock_fmg.STATE["cli_sets"] = []
ev = []
pipeline(tb, bare_cfg, Fmg(FMG, "admin", PW), lambda s_, st, m: ev.append((s_, st, m)),
         boot(scope="tpl", fmg="day0", fgt="day0", pve_secret=None, images={}))         # methods are overridden by the scope
last = {s_: st for s_, st, _ in ev if st != "info"}
assert fake_proxmox.STATE["creates"] == creates                                      # nothing on Proxmox
skip = ("pve_fmg", "bootstrap_fmg", "fmg_settings", "devices", "pve_wan", "pve_fgt", "bootstrap_fgt", "install", "clients")
assert {k: last[k] for k in skip} == dict.fromkeys(skip, "skipped"), last
assert all(last[k] == "ok" for k in ("preflight", "adom", "templates")), last
assert any(s_ == "fmg_settings" and "templates only" in m for s_, _, m in ev)
bodies = mock_fmg.STATE["bodies"]
assert mock_fmg.STATE["cli_sets"] == []                                              # FMG system settings untouched
assert not any(u == "dvm/cmd/add/device" or u.startswith("securityconsole/") for _, u, _ in bodies)
assert not any("dynamic_mapping" in str(d) for _, _, d in bodies)
assert any("pkg/adom/" in u for _, u, _ in bodies) and any("tmplgrp/adom/" in u for _, u, _ in bodies)
try:
    pipeline(tb, bare_cfg, Fmg("http://127.0.0.1:1", "admin", PW), lambda *a: None, boot(scope="tpl"))
    raise AssertionError("moest falen")
except FmgError as e:
    assert "Templates only" in str(e), e
print("OK  alleen templates: bestaande FortiManager, geen Proxmox/VM's/instellingen/devices/install; ADOM + templates gezet")

# ---------------------------------------------------------------- only the model devices, to an existing FortiManager
creates = fake_proxmox.STATE["creates"]
mock_fmg.STATE["cli"]["cli/global/system/global"]["fgfm-allow-vm"] = "disable"         # FMG 7.4.7+ default
mock_fmg.STATE["devices"].clear()
mock_fmg.STATE.pop("ifaces", None)                                                    # fresh model devices: only port1
mock_fmg.STATE["bodies"].clear()
mock_fmg.STATE["cli_sets"] = []
ev = []
pipeline(t, cfg, Fmg(FMG, "admin", PW), lambda s_, st, m: ev.append((s_, st, m)),
         boot(scope="dev", fgt_tokens={}, fgt_new_pw="", images={}))            # no FortiGate tokens/password needed
last = {s_: st for s_, st, _ in ev if st != "info"}
assert fake_proxmox.STATE["creates"] == creates                                      # nothing on Proxmox
skip = ("pve_fmg", "bootstrap_fmg", "fmg_settings", "pve_wan", "pve_fgt", "bootstrap_fgt", "clients")
assert {k: last[k] for k in skip} == dict.fromkeys(skip, "skipped"), last
assert all(last[k] == "ok" for k in ("preflight", "adom", "devices", "templates", "install")), last
bodies = mock_fmg.STATE["bodies"]
added = [d["device"] for _, u, d in bodies if u == "dvm/cmd/add/device"]
assert sorted(a["name"] for a in added) == sorted(x["name"] for x in devs), added
assert all(a["sn"] and a["device action"] == "add_model" for a in added), added      # model devices with serial
assert any("dynamic_mapping" in str(d) for _, _, d in bodies)                         # per-device values set
dns = next(d for _, u, d in bodies if u.endswith("/devprof/Basic-Settings/system/dns"))
assert dns["protocol"] == ["cleartext"] and dns["primary"] == "1.1.1.1", dns            # system template: no DoT
var_maps = [m for _, u, d in bodies if "/obj/fmg/variable/" in u for m in (d or {}).get("dynamic_mapping", [])]
assert var_maps and all(m["_scope"][0]["vdom"] == "global" for m in var_maps), var_maps[:2]   # device-level (FMG GUI)
host = next(d for _, u, d in bodies if u.endswith("/obj/fmg/variable/hostname"))
assert {m["_scope"][0]["name"] for m in host["dynamic_mapping"]} == {x["name"] for x in devs}, host   # hubs too
assert ("update", f"pm/config/adom/{t['adom']}/obj/fmg/variable/vm_interface_number", {"name": "vm_interface_number", "value": "6"}) \
    in [(m_, u, d) for m_, u, d in bodies], [b for b in bodies if "vm_interface" in b[1]]
ifs = mock_fmg.STATE["ifaces"]
assert all([i["name"] for i in ifs[x["name"]]] == [f"port{n}" for n in range(1, 7)] for x in devs), ifs  # port1-6 per model device
assert all(i["type"] == "physical" and i.get("vdom", "root") == "root" for x in devs for i in ifs[x["name"]])
n_add = sum(1 for m_, u, _ in bodies if m_ == "add" and u.endswith("/global/system/interface"))
mock_fmg.STATE["bodies"].clear()
pipeline(t, cfg, Fmg(FMG, "admin", PW), lambda *a: None, boot(scope="dev", fgt_tokens={}, fgt_new_pw="", images={}))
assert n_add == len(devs) and not any(m_ == "add" and u.endswith("/global/system/interface") for m_, u, _ in mock_fmg.STATE["bodies"])
from app.provision_vms import fgt_nics
assert all(len(fgt_nics(x, t["pve"], t["wanemu"], False)) == 6 for x in devs), [len(fgt_nics(x, t["pve"], t["wanemu"], False)) for x in devs]   # 6 NICs per FortiGate-VM
assert any(u.endswith("/object member") for _, u, _ in bodies)                        # devices in their device group
inst = [(u, d) for _, u, d in bodies if u.startswith("securityconsole/")]
dev_inst = [d["scope"][0]["name"] for u, d in inst if u == "securityconsole/install/device"]
pkg_inst = [(d["pkg"], sorted(x["name"] for x in d["scope"])) for u, d in inst if u == "securityconsole/install/package"]
assert dev_inst == [x["name"] for x in devs], dev_inst                                 # Quick install, one per device
assert sorted(pkg_inst) == sorted((r["name"], sorted(r["devices"])) for r in cfg["roles"]), pkg_inst   # Re-install policy
assert [u for u, _ in inst][:len(devs)] == ["securityconsole/install/device"] * len(devs)              # devices first
assert mock_fmg.STATE["cli_sets"] == [("cli/global/system/global", {"fgfm-allow-vm": "enable"})], mock_fmg.STATE["cli_sets"]
assert any(s_ == "devices" and "fgfm-allow-vm turned on" in m for s_, _, m in ev)        # FMG 7.4.7+: VM devices allowed
try:
    pipeline(t, cfg, Fmg("http://127.0.0.1:1", "admin", PW), lambda *a: None, boot(scope="dev"))
    raise AssertionError("moest falen")
except FmgError as e:
    assert "Model devices only" in str(e), e
print("OK  alleen model devices: bestaande FortiManager; ADOM, model devices (met serienummer) en templates met waarden per device; geen VM's/instellingen/install")

# ---------------------------------------------------------------- everything (default) is unchanged
fake_proxmox.reset()
last, _ = run()
assert names() == {"fmg", *TOKS} and all(v == "ok" for v in last.values()), (names(), last)
try:
    run(scope="bogus")
    raise AssertionError("moest falen")
except FmgError:
    pass
print("OK  alles (standaard): ongewijzigd; onbekende keuze geweigerd")

# ---------------------------------------------------------------- existing model device with another serial (FortiFlex)
import copy as _c
t_sn = _c.deepcopy(t)
t_sn["hubs"][0]["serial"] = "FGVMELTM26990001"
ev = []
pipeline(t_sn, cfg, Fmg(FMG, "admin", PW), lambda s_, st, m: ev.append((s_, st, m)), boot(scope="dev", fgt_tokens={}, images={}))
assert ("site1-H1", "FGVMELTM26990001") in mock_fmg.STATE["sn_updates"], mock_fmg.STATE.get("sn_updates")
assert any("serial number" in m and "FGVMELTM26990001" in m for _, _, m in ev)
mock_fmg.STATE["refuse_sn_update"] = True
t_sn["hubs"][0]["serial"] = "FGVMELTM26990002"
try:
    pipeline(t_sn, cfg, Fmg(FMG, "admin", PW), lambda *a: None, boot(scope="dev", fgt_tokens={}, images={}))
    raise AssertionError("moest falen")
except FmgError as e:
    assert "delete" in str(e).lower() and "site1-H1" in str(e), e
mock_fmg.STATE["refuse_sn_update"] = False
print("OK  model device met ander serienummer: bijgewerkt; weigert FortiManager dat, dan duidelijke fout")

# ---------------------------------------------------------------- LAN clients only: nothing but the client CTs
fake_proxmox.reset()
mock_fmg.STATE["bodies"].clear()
n_calls = len(mock_fmg.STATE["calls"])
last, ev = run(scope="lan", fmg="none", fgt="none", images={})
assert last["clients"] == "ok" and all(v == "skipped" for k, v in last.items() if k != "clients"), last
cts = sorted(c["params"]["hostname"] for c in fake_proxmox.STATE["cts"].values())
assert cts == ["client-site1-1", "client-site1-2", "client-site1-3"] and not fake_proxmox.STATE["vms"], cts
assert len(mock_fmg.STATE["calls"]) == n_calls, "geen FortiManager-aanroepen bij alleen LAN-clients"
print("OK  alleen LAN-clients: drie client-CT's, alle andere stappen overgeslagen, FortiManager niet aangesproken")

# ---------------------------------------------------------------- optional Traffic-Gen comes along with the LAN clients
fake_proxmox.reset()
t["trafficgen"] = {"enabled": True, "ip": "10.10.106.249/24", "count": 10}
try:
    last, ev = run(scope="lan", fmg="none", fgt="none", images={}, tg_pw="Tg-Admin-pw1!")
finally:
    t["trafficgen"] = {"enabled": False, "ip": "", "count": 10}
names_ct = sorted(c["params"]["hostname"] for c in fake_proxmox.STATE["cts"].values())
assert names_ct == ["client-site1-1", "client-site1-2", "client-site1-3", "trafficgen"], names_ct
assert last["clients"] == "ok" and any("Traffic-Gen created" in m and "https://10.10.106.249:8443" in m for _, _, m in ev), ev[-3:]
ids = sorted(int(c["vmid"]) for c in fake_proxmox.STATE["cts"].values())
assert ids == list(range(ids[0], ids[0] + 4)), ids                                          # one id block, Traffic-Gen last
print("OK  Traffic-Gen (optioneel) met de LAN-clients: eigen CT in hetzelfde id-blok, URL in de melding")

# ---------------------------------------------------------------- bridges checked before anything is created
for sc, fm, fg in (("all", "day0", "day0"), ("lan", "none", "none")):
    fake_proxmox.reset()
    fake_proxmox.STATE["bridges"] = [{"iface": "vmbr0", "type": "bridge", "bridge_ports": "eno1", "bridge_vlan_aware": 1,
                                      "bridge_vids": "301-350"}]
    try:
        run(scope=sc, fmg=fm, fgt=fg, **({"images": {}} if sc == "lan" else {}))
        raise AssertionError("expected FmgError")
    except FmgError as e:
        assert "vmbr0" in str(e) and "301-350" in str(e) and "nothing was created" in str(e), e
    assert not fake_proxmox.STATE["vms"] and not fake_proxmox.STATE["cts"] and not fake_proxmox.STATE["uploads"]
from types import SimpleNamespace as NS  # noqa: E402
from fastapi import HTTPException  # noqa: E402
rq = lambda sc, fg="day0": NS(scope=sc, bootstrap=NS(fgt=fg), secrets=NS(pve_token_secret="pve-secret"))
for sc in ("all", "fgt", "lan"):                 # the API checks too, before FortiFlex creates any licence
    try:
        main._check_bridges(t, rq(sc))
        raise AssertionError("expected 422")
    except HTTPException as e:
        assert e.status_code == 422 and any("301-350" in x for x in e.detail), e.detail
for sc in ("fmg", "tpl", "dev"):
    main._check_bridges(t, rq(sc))               # no FortiGate VMs / clients: bridges not asked
fake_proxmox.STATE["bridges"] = None
main._check_bridges(t, rq("all"))                # default fake bridges are fine
# uplink untagged while the host's own network on that bridge is another one: refused up front, no default VLAN
fake_proxmox.STATE["bridges"] = [{"iface": "vmbr0", "type": "bridge", "bridge_ports": "eno1", "cidr": "10.10.100.13/24"}]
tu = copy.deepcopy(t); tu["wanemu"]["uplink"]["vlan"] = None
try:
    main._check_bridges(tu, rq("all"))
    raise AssertionError("expected 422")
except HTTPException as e:
    assert any("fill in the Uplink VLAN" in x and "10.10.100.0/24" in x for x in e.detail), e.detail
main._check_bridges(t, rq("all"))                # with its VLAN (106 in this lab): fine
fake_proxmox.STATE["bridges"] = None
print("OK  bridges vooraf gecontroleerd: VLAN buiten bridge-vids -> duidelijke fout, niets aangemaakt (alles en LAN-clients; ook in de API vóór FortiFlex)")

# ---------------------------------------------------------------- API: scope overrides the methods + validation
def req(scope, **sec):
    s = {"fgt_tokens": {}, "fgt_new_pw": "", **sec}
    return main.DeployReq(topology={}, fmg=main.Fmgcfg(password=PW), scope=scope, bootstrap={"fmg": "day0", "fgt": "day0"},
                          secrets=s, images={})


r = req("fmg")
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("day0", "none")
r = req("fgt")
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("none", "day0")
r = req("tpl")
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("none", "none")
try:
    main._apply_scope(req("x"))
    raise AssertionError("moest falen")
except HTTPException as e:
    assert e.status_code == 422


def errors(r):
    try:
        main._check_bootstrap(t, r)
        return []
    except HTTPException as e:
        return e.detail


r = req("tpl")
main._apply_scope(r)
assert errors(r) == [], errors(r)                                                    # only the FMG password: no Proxmox, tokens, images
r = req("dev")
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("none", "none") and errors(r) == [], errors(r)   # no Proxmox, tokens, images
r = req("fgt", fgt_tokens=TOKS, fgt_new_pw=PW, pve_token_secret="x")
main._apply_scope(r)
e = errors(r)
assert not any("FortiManager: FortiFlex" in x or "FortiManager: image" in x for x in e), e      # FMG token/image not needed
r = req("fmg", fmg_token="FMGTOKEN0123456789", pve_token_secret="x")
main._apply_scope(r)
e = errors(r)
assert not any("FortiFlex token missing" in x and "FortiManager" not in x for x in e), e     # FGT tokens not needed
assert not any("FortiGates: admin password" in x or "FortiGate: image" in x for x in e), e
r = req("fgt", fgt_tokens=TOKS, fgt_new_pw=PW, pve_token_secret="x")
r.fmg.password = None
main._apply_scope(r)
assert any("FortiManager: admin password" in x for x in errors(r))                          # API login always needed
# strong passwords for what the tool sets (new FortiManager, FortiGates); an existing FortiManager's login is as it is
from app import model as _m  # noqa: E402
assert _m.password_problems(PW) == []
assert set(_m.password_problems("abc")) == {"at least 8 characters", "an uppercase letter", "a digit", "a special character"}
assert _m.password_problems("Abcdefg1") == ["a special character"] and _m.password_problems("ABCDEFG1!") == ["a lowercase letter"]
r = req("fgt", fgt_tokens=TOKS, fgt_new_pw="welkom123", pve_token_secret="x")
main._apply_scope(r)
assert any(x.startswith("FortiGates: admin password is not strong") and "uppercase" in x for x in errors(r)), errors(r)
r = req("fmg", fmg_token="FMGTOKEN0123456789", pve_token_secret="x")
r.fmg.password = "admin"
main._apply_scope(r)
assert any(x.startswith("FortiManager: admin password is not strong") for x in errors(r)), errors(r)
r = req("tpl")
r.fmg.password = "admin"                                                                    # existing FortiManager
main._apply_scope(r)
assert errors(r) == [], errors(r)
t["trafficgen"] = {"enabled": True, "ip": "10.10.106.249/24", "count": 10}
try:
    r = req("lan", pve_token_secret="x", client_pw="Client-pw1", tg_pw="zwak")
    main._apply_scope(r)
    e = errors(r)
    assert any(x.startswith("Traffic-Gen: GUI password is not strong") for x in e), e
    r = req("lan", pve_token_secret="x", client_pw="Client-pw1", tg_pw="Tg-Admin-pw1!")
    main._apply_scope(r)
    assert not any("Traffic-Gen" in x for x in errors(r)), errors(r)
    t["trafficgen"]["ip"] = "10.99.0.5/24"
    assert any("uplink network" in x for x in errors(r)), errors(r)
finally:
    t["trafficgen"] = {"enabled": False, "ip": "", "count": 10}
print("OK  sterk wachtwoord verplicht voor nieuwe FortiManager en FortiGates (8+, hoofd/kleine letter, cijfer, teken); bestaande FMG niet")
# LAN clients only: Proxmox + a root password; no FortiManager, no tokens, no images
r = req("lan", pve_token_secret="x", client_pw="Client-pw1")
r.fmg.password = None
main._apply_scope(r)
assert (r.bootstrap.fmg, r.bootstrap.fgt) == ("none", "none")
e = errors(r)
assert not any("FortiManager" in x or "FortiFlex" in x or "image" in x for x in e), e
r = req("lan", pve_token_secret="x", fgt_new_pw=PW)                                         # FortiGate password is not it
main._apply_scope(r)
assert any("LAN client" in x and "password" in x for x in errors(r)), errors(r)              # own client root password
r = req("fgt", fgt_tokens=TOKS, fgt_new_pw=PW, pve_token_secret="x")                          # FortiGate VMs come with clients
main._apply_scope(r)
assert any("LAN client" in x and "password" in x for x in errors(r)), errors(r)
r = req("tpl")
main._apply_scope(r)
assert not any("LAN client" in x for x in errors(r))
print("OK  API: keuze zet de methode van het andere product op 'none'; alleen de benodigde tokens/images/wachtwoorden verplicht")
print("alle scope-tests geslaagd")
