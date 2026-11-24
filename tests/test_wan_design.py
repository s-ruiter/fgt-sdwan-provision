"""WAN design: hub = one wan (wan1, static); branch = wan1 + optional wan2, both DHCP; every branch wan has a tunnel to
wan1 of every hub. Checks the rendered templates (no servers needed).  python tests/test_wan_design.py"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fmgtemplates as ft, model  # noqa: E402

ORCH = os.environ.get("ORCH_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "ref"))


def topo(multi=True, **extra):
    t = model.merged(dict(extra, region=dict(model.DEFAULTS["region"], multi_vrf=multi)))
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM26{i:06d}"
    for i, h in enumerate(t["hubs"], start=1):
        h["wan1_gw"] = "100.127.200.1"
    return t


def tunnels(cli):
    out = []
    for blk in re.findall(r"config vpn ipsec phase1-interface\n((?:.|\n)*?)\nend", cli):
        for m in re.finditer(r'edit "([^"]+)"((?:.|\n)*?)\n  next', blk):
            g = lambda k, b=m.group(2): (re.search(rf"set {k} (\S+)", b) or [None, None])[1]
            out.append((m.group(1), g("interface"), g("remote-gw")))
    return out


# 1. old input (GUI/API/CSV) is forced into the design
t = model.merged({"hubs": [dict(model.DEFAULTS["hubs"][0], wan1_mode="dhcp", wan2_intf="port2", wan2_ip="1.2.3.4"),
                           model.DEFAULTS["hubs"][1]],
                  "edges": [dict(model.DEFAULTS["edges"][0], wan1_mode="static", wan1_ip="100.64.9.1/29", wan1_gw="100.64.9.6")]})
h, e = t["hubs"][0], t["edges"][0]
assert h["wan1_mode"] == "static" and not any(k in h for k in ("wan2_intf", "wan2_ip", "wan1_hub_ip")), h
assert e["wan1_mode"] == "dhcp" and not any(k in e for k in ("wan1_ip", "wan1_gw")) and e["wan2_intf"] == "port2", e
print("OK  invoer wordt afgedwongen: hub alleen wan1 statisch, branch wan1/wan2 DHCP")

# 2. validation: hub needs a static wan1 (+ gateway when devices are deployed); branch needs no address
t = topo()
assert model.validate(t) == [], model.validate(t)
t["hubs"][0]["wan1_ip"] = ""
assert model.merged(t)["hubs"][0]["wan1_ip"] == "100.64.1.2/24"            # the hub address is derived (own /24)
t = topo()
t["hubs"][0]["wan1_gw"] = ""                       # the hub gateway always comes from ISP-HUB (wan-emu)
assert model.validate(t) == [] and t["hubs"][0]["wan1_gw"] == "100.64.1.1"
t = topo()
t["edges"][0]["wan2_intf"] = t["edges"][0]["wan1_intf"]
assert any("another port" in x for x in model.validate(t))
print("OK  validatie: hub-wan1 IP verplicht (gateway bij devices), branch zonder adres, wan2 != wan1")

# 3. rendered templates, multi- and single-VRF
for multi in (True, False):
    t = topo(multi)
    plan = ft.build_plan(t, ORCH)
    hub_ip = {h["name"]: model.hub_wan1_ip(h) for h in t["hubs"]}
    for r in plan["roles"]:
        cli = "\n".join(text for _, text, _ in r["cli"])
        tun = tunnels(cli)
        members = [m["interface"] for m in r["sdwan"]["members"]]
        if r["kind"] == "hub":
            assert [x[0] for x in tun] == ["EDGE_WAN1"], tun                     # one dial-up tunnel, on wan1
            assert members == ["port1", "EDGE_WAN1"], members
            assert "wan2" not in cli.lower() and "port2" not in str(r["sdwan"]), r["name"]
            assert 'edit "Lo-HC"' in cli and "10.200.99.1/32" in cli                 # health-check loopback
            hub = next(h for h in t["hubs"] if h["name"] == r["devices"][0])
            assert r["sdwan"]["members"][0].get("gateway") == hub["wan1_gw"], r["sdwan"]["members"][0]   # gateway on member
            for blk in re.findall(r"config router static\n((?:.|\n)*?)\nend", cli):   # no route on wan1 next to the
                assert '"$(wan1_intf)"' not in blk and hub["wan1_intf"] not in blk, blk  # SD-WAN default (dup 1/100)
            assert [s["dst"] for s in r["static"]][0] == ["0.0.0.0", "0.0.0.0"] and r["static"][0].get("sdwan-zone"), r["static"]
            # Day-0 builds the same SD-WAN default as the template, so the install edits it in place (no dup 1/100)
            from app import bootstrap as bs
            m0 = r["sdwan"]["members"][0]
            assert (m0["zone"], m0["seq-num"]) == (bs.HUB_UNDERLAY_ZONE, bs.HUB_WAN1_MEMBER), m0
            assert (r["static"][0]["seq-num"], r["static"][0]["sdwan-zone"]) == (bs.HUB_DEFAULT_ROUTE, [bs.HUB_UNDERLAY_ZONE])
            day0 = bs.fgt_network_lines(dict(hub, wan1_mode="static"), t)
            assert "set device" not in "\n".join(day0) and f"edit {bs.HUB_DEFAULT_ROUTE}" in day0, day0
        else:
            dev = next(d for d in t["edges"] if d["name"] == r["devices"][0])
            wans = ["$(wan1_intf)"] + (["$(wan2_intf)"] if dev.get("wan2_intf") else [])
            want = sorted((f'"{w}"', ip) for ip in hub_ip.values() for w in wans)    # every wan -> wan1 of every hub
            assert sorted((x[1], x[2]) for x in tun) == want, (r["name"], tun)
            want_names = sorted(f"H{i}-WAN{w}" for i in range(1, len(t["hubs"]) + 1) for w in range(1, len(wans) + 1))
            assert sorted(x[0] for x in tun) == want_names, tun                        # <hub>-WAN<branch wan>
            for x in tun:                                                               # H1-WAN2 leaves from wan2
                assert x[1] == f'"$(wan{x[0][-1]}_intf)"', x
            blob = cli + str(r["sdwan"]) + str(r["policies"]) + str(r["static"])
            assert not re.search(r"H\d_WAN1", blob), re.findall(r"H\d_WAN1\w*", blob)   # no Orchestrator names left
            assert [m["interface"] for m in r["sdwan"]["members"]][len(wans):] == sorted(want_names, key=lambda n: (n[1], n[-1]))
            hc = next(h for h in r["sdwan"]["health-check"] if h["name"] == "HUB")
            assert hc["server"] == "10.200.99.1" and len(hc["members"]) == len(want), hc
            for w in wans:                                                             # branch wans: DHCP only
                blk = re.search(rf'edit "{re.escape(w)}"\n((?:    .*\n)*?)  next', cli).group(1)
                assert "set mode dhcp" in blk and "set mode static" not in blk, (r["name"], w, blk)
print("OK  templates (multi- en single-VRF): hub alleen EDGE_WAN1 + Lo-HC; elke branch-WAN een tunnel naar wan1 van elke hub")
print("alle WAN-ontwerp-tests geslaagd")

# FortiManager host route on every role: the template's blackhole 10.0.0.0/8 (summary) must not swallow the FGFM tunnel
for multi in (True, False):
    t = topo(multi)
    t["fmgr"]["ip"] = "10.10.106.11/24"
    plan = ft.build_plan(t, ORCH)
    for r in plan["roles"]:
        fr = [x for x in r["static"] if x.get("dst") == ["10.10.106.11", "255.255.255.255"]]
        assert len(fr) == 1 and fr[0]["sdwan-zone"] == ["underlay"] and fr[0]["seq-num"] == ft.FMG_ROUTE_SEQ, (r["name"], r["static"])
        assert len({x["seq-num"] for x in r["static"]}) == len(r["static"])
        cli = "\n".join(text for _, text, _ in r["cli"])
        assert f"edit {ft.FMG_ROUTE_SEQ}\n" not in cli, r["name"]                      # no clash with the CLI templates' routes
print("OK  hostroute naar FortiManager via underlay op elke rol (niet in de blackhole 10.0.0.0/8 van het template)")
