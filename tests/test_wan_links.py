"""One VLAN + /24 per FortiGate WAN: hub n wan1 100.64.n.0/24 (VLAN 3200+n), spoke n wan1 100.65.n.0/24 (3210+n),
spoke n wan2 100.66.n.0/24 (3230+n); FortiGate .2, wan-emu .1. All traffic of a WAN (shortcuts too) then passes the
wan-emu."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model, settings_io  # noqa: E402
from tests import labuplink  # noqa: E402,F401


def lab(extra=None):
    t = model.merged(extra or {})
    t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
    return t


t = lab()
L = {x["id"]: x for x in model.wan_links(t)}
assert list(L) == ["site1-H1-wan1", "site1-H2-wan1", "site1-1-wan1", "site1-1-wan2", "site1-2-wan1", "site1-3-wan1",
                   "site1-3-wan2"], list(L)
assert L["site1-H1-wan1"] == {"id": "site1-H1-wan1", "name": "site1-H1", "seg": "hub", "n": 1, "subnet": "100.64.1.0/24",
                              "gw": "100.64.1.1", "ip": "100.64.1.2", "vlan": 3201}
assert L["site1-H2-wan1"]["vlan"] == 3202 and L["site1-1-wan1"]["ip"] == "100.65.1.2" and L["site1-1-wan1"]["vlan"] == 3211
assert L["site1-1-wan2"]["ip"] == "100.66.1.2" and L["site1-1-wan2"]["vlan"] == 3231
assert L["site1-3-wan2"]["ip"] == "100.66.3.2" and L["site1-3-wan2"]["vlan"] == 3233 and "site1-2-wan2" not in L
assert t["hubs"][0]["wan1_ip"] == "100.64.1.2/24" and t["hubs"][0]["wan1_gw"] == "100.64.1.1"
assert t["hubs"][1]["wan1_ip"] == "100.64.2.2/24" and model.hub_wan1_ip(t["hubs"][1]) == "100.64.2.2"
assert t["wanemu"]["segments"]["a"] == {"name": "ISP-A", "block": "100.65.0.0/16", "vlan_base": 3210}
assert [e for e in model.validate_wanemu(t)] == [], model.validate_wanemu(t)
print("OK  links: hub/spoke wan1/wan2 eigen /24 en VLAN, hub-adres afgeleid, standaard geldig")


def errs(seg_over=None, **w):
    x = lab({"wanemu": dict(w, **({"segments": seg_over} if seg_over else {}))})
    return " | ".join(model.validate_wanemu(x))


assert "ISP-A: block" in errs({"a": {"block": "nope"}})
assert "ISP-A: block" in errs({"a": {"block": "100.65.0.0/24"}})              # needs room for /24s per spoke
assert "ISP-A: block" in errs({"a": {"block": "100.65.0.0/23"}})               # .0 and .1 = only spoke 1
assert "overlap" in errs({"a": {"block": "100.64.0.0/16"}})                    # same as ISP-HUB
assert "overlap" in errs({"a": {"block": "10.10.0.0/16"}})                     # covers the uplink 10.10.106.0/24
assert "VLAN" in errs({"a": {"vlan_base": 3200}})                              # 3201.. clashes with the hubs
assert "VLAN" in errs({"a": {"vlan_base": 3299}})                              # 3300.. = LAN VLANs
many = lab({"edges": [dict(model.DEFAULTS["edges"][0], name=f"s{i}", loopback=f"10.200.1.{i + 1}") for i in range(15)]})
assert any("31" in e for e in model.validate_wanemu(many)), model.validate_wanemu(many)      # 2 + 30 = 32 links
print("OK  validatie: blok, overlap, VLAN-botsing, maximaal 31 WAN-lijnen")

old = {"name": "ISP-A", "vlan": 3201, "subnet": "100.127.201.0/24", "gw": "100.127.201.1", "res_start": 11, "pool": [100, 199]}
m = lab({"wanemu": {"segments": {"a": old}}})
assert m["wanemu"]["segments"]["a"] == {"name": "ISP-A", "block": "100.65.0.0/16", "vlan_base": 3210}, m["wanemu"]["segments"]["a"]
f = {"format": "fgt-sdwan-settings", "version": 1, "topology": {"wanemu": {"segments": {"a": old}}}}
topo, ui, notes = settings_io.from_file(f, {})
assert topo["wanemu"]["segments"]["a"]["block"] == "100.65.0.0/16" and any("one subnet per WAN" in n for n in notes), notes
print("OK  oude instellingen (subnet/gw per segment): nieuwe standaard + melding")

# ---- wan-emu config: one NIC + DHCP per link, FortiGate NICs on the link VLAN, FMG routes per ISP block
from app import provision_vms as pv  # noqa: E402
from app.wanemu import net, setup  # noqa: E402

t = lab()
ln = net.links(t)
assert [x["iface"] for x in ln] == [f"eth{k}" for k in range(1, 8)] and ln[2]["id"] == "site1-1-wan1"
ct = net.ct_params(t, 130, "k")
br = t["wanemu"]["bridge"]
assert ct["net3"].startswith(f"name=eth3,bridge={br},tag=3211,ip=100.65.1.1/24,firewall=0"), ct["net3"]
assert ct["net1"].startswith(f"name=eth1,bridge={br},tag=3201,ip=100.64.1.1/24,firewall=0") and "net8" not in ct
dm = net.dnsmasq_conf(t)
assert "interface=eth3" in dm and "dhcp-range=set:eth3,100.65.1.100,100.65.1.199,255.255.255.0,12h" in dm, dm
assert "dhcp-option=tag:eth3,option:router,100.65.1.1" in dm
res = {r["ip"]: r for r in net.reservations(t)}
assert set(res) == {"100.65.1.2", "100.66.1.2", "100.65.2.2", "100.65.3.2", "100.66.3.2"}
assert f"dhcp-host={res['100.65.1.2']['mac']},100.65.1.2" in dm
assert "interface=eth1" not in dm and "interface=eth2" not in dm                  # hubs: static, no DHCP
up = t["wanemu"]["uplink"]["ip"].split("/")[0]
assert net.fmg_routes(t) == [("100.64.0.0", "255.255.0.0", up), ("100.65.0.0", "255.255.0.0", up),
                             ("100.66.0.0", "255.255.0.0", up)]
fw = {f["name"]: f["ip"] for f in net.forwards(t)}
assert fw["site1-1"] == "100.65.1.2" and fw["site1-H1"] == "100.64.1.2"
need = net.bridge_needs(t)[br]
assert {3201, 3202, 3211, 3212, 3213, 3231, 3233} <= need and not {3200, 3210, 3230, 3232} & need, sorted(need)
topo = net.agent_topology(t)
assert topo["nat"] == ["100.64.0.0/16", "100.65.0.0/16", "100.66.0.0/16"] and topo["links"][2]["iface"] == "eth3"
nics = {n: pv.fgt_nics(d, t["pve"], t["wanemu"], hub, index=n, link_no=n + 1 if hub else n - 1)
        for n, (d, hub) in enumerate(net.fortigates(t))}
assert nics[0][0]["vlan"] == 3201 and nics[1][0]["vlan"] == 3202
assert nics[2][0]["vlan"] == 3211 and nics[2][1]["vlan"] == 3231 and nics[3][0]["vlan"] == 3212
assert nics[2][0]["mac"] == res["100.65.1.2"]["mac"]


class Sh:
    def __init__(self):
        self.cmds = []

    def run(self, c):
        self.cmds.append(c)
        return 0, "", ""


sh = Sh()
setup.internet_check(sh, t)
assert "-I 100.65.1.1" in sh.cmds[0], sh.cmds
# the wan-emu NICs keep their MAC across re-deploys (the FortiGates' ARP entry for .1 stays valid)
ct2 = net.ct_params(lab(), 131, "k")
assert all("hwaddr=" in ct[f"net{k}"] for k in range(1, 8)) and ct["net3"] == ct2["net3"]
assert len({ct[f"net{k}"].split("hwaddr=")[1].split(",")[0] for k in range(1, 8)}) == 7
print("OK  wan-emu: NIC + DHCP per lijn, FortiGate-NIC's op het lijn-VLAN, FMG-routes per ISP-blok, forwards")

# ---- re-deploy: a reused wan-emu gets the new NICs (old extra ones deleted); FortiGates of the old layout -> Cleanup
from app.pve import Pve, PveError  # noqa: E402
from tests import fake_proxmox as fp, fake_wanemu  # noqa: E402

fp.reset()
srv = fp.start(8091)
try:
    pve = Pve("http://127.0.0.1:8091", "root@pam!t", "secret", poll=0)
    S = fp.STATE
    t = lab()
    t["pve"]["node"] = "pve1"
    old = {f"net{k}": f"name=eth{k},bridge=vmbr1,tag={3199 + k}" for k in range(1, 10)}       # 9 NICs before
    S["cts"]["121"] = {"vmid": 121, "params": dict(old, hostname="wan-emu", tags="fgt-sdwan"), "status": "running"}
    sh = fake_wanemu.FakeShell(installed=True)
    setup.deploy_wanemu(pve, t, {"ssh_key": setup.new_ssh_key(), "agent_token": "tok1234567890abcdef"}, lambda *a: None,
                        connector=fake_wanemu.connector(sh), waiter=lambda *a: True)
    p = S["cts"]["121"]["params"]
    assert p["net3"].startswith("name=eth3,") and "tag=3211" in p["net3"] and "net8" not in p and "net9" not in p, p
    # FortiGate VM of the old layout (wan on 3201 = old ISP-A) where site1-1 wan1 now needs 3211
    S["vms"]["130"] = {"vmid": 130, "name": "site1-1", "status": "running",
                       "params": {"tags": "fgt-sdwan", "net0": "virtio=02:AA:BB:CC:DD:01,bridge=vmbr1,tag=3201"}}
    specs = [s for s in pv.build_specs(t, "pw", "pw", {}, None, "fgt") if s["name"] == "site1-1"]
    before = dict(S["vms"]["130"]["params"])
    try:
        pv.deploy_vms(pve, t["pve"] | {"node": "pve1"}, specs, {}, lambda *a: None, "pve_fgt", {}, start=False)
        raise AssertionError("expected an error for the old WAN layout")
    except PveError as e:
        assert "VLAN 3201" in str(e) and "needs VLAN 3211" in str(e) and "Cleanup" in str(e), str(e)
    assert S["vms"]["130"]["params"] == before
    # the CT config cannot be read: an error, not silently keeping stale NICs
    S["fail_ct_config_get"] = True
    try:
        setup.deploy_wanemu(pve, t, {"ssh_key": setup.new_ssh_key(), "agent_token": "tok1234567890abcdef"}, lambda *a: None,
                            connector=fake_wanemu.connector(sh), waiter=lambda *a: True)
        raise AssertionError("expected WanEmuError")
    except setup.WanEmuError as e:
        assert "configuration" in str(e), str(e)
    S["fail_ct_config_get"] = False
finally:
    srv.shutdown()
print("OK  opnieuw uitrollen: wan-emu krijgt nieuwe NIC's (oude extra weg); FortiGate in oude opzet -> eerst Cleanup")
