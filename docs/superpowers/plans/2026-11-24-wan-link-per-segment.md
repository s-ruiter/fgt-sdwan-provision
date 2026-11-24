# One segment per FortiGate WAN Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every FortiGate WAN gets its own VLAN and /24 (routed by the wan-emu), so the WAN emulation also applies to ADVPN spoke-to-spoke shortcuts.

**Architecture:** One function derives every link (address, gateway, VLAN, CT interface) from the topology; model, net (wan-emu config), agent (shaping per interface), VM NICs, FMG routes and the UI all use it. The agent shapes per link interface instead of per IP on a shared segment.

**Tech Stack:** Python (FastAPI), Proxmox API, LXC + tc/nftables/dnsmasq, vanilla JS/SVG.

**Spec:** `docs/superpowers/specs/2026-11-24-wan-link-per-segment-design.md`

## Global Constraints
- Address plan: hub n wan1 = `100.64.n.0/24`, FGT `.2` (static), gw `.1`, VLAN `3200+n`; spoke n wan1 = `100.65.n.0/24` `.2` (DHCP reservation) VLAN `3210+n`; spoke n wan2 = `100.66.n.0/24` `.2` VLAN `3230+n`. n = 1-based position in the topology (hubs / edges).
- Per ISP in `wanemu.segments.{hub,a,b}`: `name`, `block` (`100.64.0.0/16`, `100.65.0.0/16`, `100.66.0.0/16`), `vlan_base` (3200, 3210, 3230). `subnet`/`gw`/`vlan`/`res_start`/`pool` are gone.
- wan-emu: `net0` = uplink (eth0), `net1..netK` = one per link, `name=eth<K>`, `tag=<link vlan>`, `ip=<gw>/24`, `firewall=0`. Proxmox max 32 LXC NICs (`$MAX_LXC_NETWORKS = 32` on PVE01) -> at most 31 links.
- Link ids stay `<device>-wan1` / `<device>-wan2`; agent API unchanged.
- No `@` handles in commit messages; each commit ends with the Co-Authored-By line.

## Review Focus
- Re-deploy over a deployment in the OLD layout (reused FortiGate VMs on 3200/3201/3202): stop before changing anything, with "deployed with the old WAN layout: run Cleanup first" (Task 4).
- An old saved settings file / last-used form with `subnet`/`gw` per segment -> the new defaults are used, not a crash (Task 1).
- A spoke without wan2 -> no ISP-B link, no NIC, no VLAN for it, numbering of the other spokes unchanged (Task 2).
- More than 31 links or a VLAN range that overlaps the LAN VLANs/uplink -> a validation error before anything is created (Task 1).
- `internet_check` and anything else that used `segments["a"]["gw"]` -> uses the first spoke link's gateway (Task 2).

---

### Task 1: Model: per-ISP blocks, derived link addresses, hub address, validation, old settings
**Files:** Modify `app/model.py`, `app/settings_io.py`; Test `tests/test_wan_links.py` (new), adjust `tests/test_wan_design.py`, `tests/test_settings.py`.

**Interfaces:** Produces `model.wan_links(t) -> list[dict]` with, in order hubs then edges (wan1, then wan2): `{"id","name","seg":"hub"|"a"|"b","n":int,"subnet":"a.b.c.0/24","gw":"a.b.c.1","ip":"a.b.c.2","vlan":int}`; `model.MAX_WAN_LINKS = 31`. `normalize_wans` sets hub `wan1_ip = "<ip>/24"`, `wan1_gw = <gw>` from `wan_links`. `merged()` replaces a segment dict that lacks `block` by the default (and keeps `name`).

- [ ] Test: defaults -> hub 1 `100.64.1.2`/`100.64.1.1`/VLAN 3201, hub 2 VLAN 3202; edge 1 wan1 `100.65.1.2` VLAN 3211, edge 1 wan2 `100.66.1.2` VLAN 3231; edge 2 (site1-2, no wan2) has only a wan1 link, edge 3 wan2 = `100.66.3.2` VLAN 3233; hub `wan1_ip == "100.64.1.2/24"` after `normalize_wans`. Validation errors for: block not a network / prefix > /23 / too small for n; two blocks overlapping; block overlapping the uplink subnet; VLAN range overlapping another ISP or the LAN range or the uplink VLAN; 32 links (`> MAX_WAN_LINKS`). Old segment `{"name":"ISP-A","vlan":3201,"subnet":"100.127.201.0/24","gw":"100.127.201.1"}` -> merged gives the default block/vlan_base; settings import of such a file returns a note containing "one subnet per WAN".
- [ ] FAIL -> implement (also update `validate_wanemu`; drop res_start/pool checks) -> PASS -> commit.

### Task 2: wan-emu config (net.py), FortiGate NICs, FMG routes, bridge needs
**Files:** Modify `app/wanemu/net.py`, `app/wanemu/setup.py` (`internet_check`), `app/provision_vms.py` (`fgt_nics`), `app/bootstrap.py` (FMG routes count/seq), `app/fmg.py` (log lines that print segments); Tests `tests/test_wanemu_net.py`, `tests/test_wanemu_fwd.py`, `tests/test_bootstrap.py`, `tests/test_day0_units.py` (replace 100.127 expectations).

**Interfaces:** Consumes `model.wan_links`. `net.links(t)` = `wan_links` + `"iface": "eth<K>"` (K = 1-based position). `net.reservations(t)` = spoke links with `mac` (`mac_for(name, port)`) and `ip` (.2). `net.dnsmasq_conf` per spoke link interface: range `.100-.199` of its /24, router = gw, `dhcp-host=<mac>,<ip>`. `net.ct_params` net1..netK per link. `net.fmg_routes` = one route per ISP block. `net.agent_topology` = `{"links": links, "nat": [3 blocks], "no_nat": [...], "forwards": [...]}`. `net.bridge_needs` lists every link VLAN on the WAN bridge. `fgt_nics` uses the link VLAN of that device's wan port.

- [ ] Tests: default topology -> 7 links, ifaces eth1..eth7; `ct_params["net3"] == "name=eth3,bridge=<br>,tag=3211,ip=100.65.1.1/24,firewall=0"`; dnsmasq has `interface=eth3`, `dhcp-range=set:eth3,100.65.1.100,100.65.1.199,255.255.255.0,12h`, `dhcp-option=tag:eth3,option:router,100.65.1.1`, `dhcp-host=<mac>,100.65.1.2`, and no range on hub interfaces; `fmg_routes` == 3 routes `(100.64.0.0,255.255.0.0,<uplink ip>)` etc.; forwards ip for site1-1 = `100.65.1.2`; bridge_needs contains 3201,3202,3211-3213,3231,3233; `fgt_nics` site1-1 port1 tag 3211, port2 tag 3231, hub port1 3201; `internet_check` uses `100.65.1.1`.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 3: Agent: shaping and link down per interface
**Files:** Modify `app/wanemu/agent.py`; Test `tests/test_wanemu_agent.py`.

**Interfaces:** `tc_commands(topo, state)`: per link `iface`: root `htb` default class `1:1` (rate = link rate or `10gbit`) with netem leaf, ingress `matchall` mirred to `ifb-<iface>` with the same htb+netem; no `u32` filters. `nft_ruleset`: link down = `iifname "<iface>" drop` + `oifname "<iface>" drop` in forward; masquerade `oifname "eth0" ip saddr { blocks }` minus lab; port forward masquerade `ct status dnat oifname { link ifaces }`.

- [ ] Tests: two links -> commands contain `tc qdisc replace dev eth1 root handle 1: htb default 1`, a netem `delay 100ms` leaf on `eth1` and `ifb-eth1` for a 100 ms link, no `u32`; link down -> `iifname "eth3" drop`; NAT line lists the three blocks.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 4: Re-deploy: wan-emu NICs replaced, old-layout FortiGates refused
**Files:** Modify `app/wanemu/setup.py` (`deploy_wanemu` reuse: delete netK beyond the new count), `app/provision_vms.py` (`deploy_vms` reuse check); Tests `tests/test_wanemu_pipeline.py`, `tests/test_name_prefix.py` or new cases in `tests/test_wan_links.py` with the fake Proxmox.

**Interfaces:** `deploy_vms` raises `PveError`-wrapped message "…deployed with the old WAN layout (VLAN …): run Cleanup first" when a reused FortiGate VM's wan NIC tag differs from `fgt_nics` (checked via `GET /nodes/{n}/qemu/{id}/config` before anything is changed).

- [ ] Tests: reused wan-emu CT with net1..net3 -> config set with net1..net7 and `delete` empty; a CT with net1..net9 (more links before) -> `delete=net8,net9`; a reused FortiGate VM with `net0` tag 3201 where 3211 is planned -> error mentioning "Cleanup", no VM changed.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 5: UI: WAN emulation form, hub card, topology diagram, port roles
**Files:** Modify `static/index.html`, `static/app.js`; Test `tests/test_ui_static.py`.

- [ ] Static test: form ids `wan-seg-<k>-block`, `wan-seg-<k>-vlanbase` for hub/a/b; no `wan-seg-*-subnet`, `wan-seg-*-gw`, `wan-seg-*-res`, no `wan1_oct`/`data-hubpre`; hub card shows the derived wan1 address read-only (`data-hubaddr`); `topoSvg` labels each ISP bar with block and VLAN range (`VLAN ${lo}–${hi}`) and each FortiGate box with `WAN1 <ip> · VLAN <v>`; `portRoles` shows the VLAN range per ISP.
- [ ] FAIL -> implement (a JS `wanLinks(t)` mirroring `model.wan_links`) -> PASS -> check in the browser against a local uvicorn (diagram, form) -> commit.

### Task 6: Remaining tests and docs
- [ ] `grep -rn "100\.127" app static tests` -> only intentional (old-settings test) hits; README/About text updated; all tests green (`alltests.sh`); commit.

### Task 7: Final review, PR, lab check
- [ ] Whole-branch review, fixes, PR. After merge/update and a new deployment: ping client site1-1 -> client site1-3 with 100 ms on site1-1 wan1+wan2 stays ~200 ms after the shortcut is up; with both down the shortcut stops; internet sliders still work.
