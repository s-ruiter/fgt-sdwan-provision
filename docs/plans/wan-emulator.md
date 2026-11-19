# WAN-emulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** De deploy-tool rolt een LXC `wan-emu` uit die ISP-HUB/ISP-A/ISP-B routeert, DHCP doet voor de spokes en per link latency/jitter/loss/bandbreedte/down toepast, met een GUI-tab om dat aan te sturen.

**Architecture:** Pure functies in `app/wanemu/` rekenen alles uit de topologie (MAC's, reserveringen, links, dnsmasq-config, LXC-params). De pipeline-stap `pve_wan` maakt de CT via de Proxmox-API en richt hem via SSH (paramiko) in. In de CT draait een stdlib-agent (`agent.py`) die `tc`/`nft` uitvoert; de tool proxyt de GUI naar die agent.

**Tech Stack:** Python 3 (FastAPI, paramiko, requests), Proxmox VE 9 API, Debian 13 LXC, dnsmasq, iproute2 `tc` (htb/netem/ifb/mirred), nftables, vanilla JS UI.

**Spec:** `docs/specs/wan-emulator.md`

## Global Constraints

- Segmenten: ISP-HUB VLAN 3200 `100.127.200.0/24` gw `100.127.200.1`; ISP-A VLAN 3201 `100.127.201.0/24` gw `100.127.201.1`; ISP-B VLAN 3202 `100.127.202.0/24` gw `100.127.202.1`.
- Uplink: VLAN 106, `10.10.106.250/24`, gateway `10.10.106.1`, bridge `vmbr0`.
- Hubs: site1-H1 `100.127.200.11/24`, site1-H2 `100.127.200.12/24`, gateway `100.127.200.1`.
- DHCP: reserveringen vanaf `.11` (spoke n → .11+n), pool `.100`–`.199`, lease 12 h; max 89 spokes.
- **NAT (masquerade) alleen op eth0** naar het lab; tussen de segmenten geen NAT. **Geen aan/uit-schakelaar**: altijd aan bij FortiGate-methode `day0`.
- De tool wijzigt de lab-gateway 10.10.106.1 en de PVE-host (kernelmodules) niet; hij controleert en meldt.
- Agent: stdlib only, poort `8090`, bearer-token; staat in `/etc/wan-emu/{topology,links}.json`.
- MAC's: locally administered `02:…`, stabiel per (devicenaam, poort).
- Geen wachtwoorden/tokens/privésleutels in events, logs of commits (bestaande `Masker`).
- Tests volgen de bestaande stijl: losse scripts `tests/test_*.py` met `assert` en `print("OK  ...")`, uitvoerbaar met `python -W ignore tests/test_x.py` (ORCH_DIR gezet waar templates nodig zijn).
- Teksten in de UI en meldingen in het Nederlands.

## Review Focus

1. **Bestaande CT met andere instellingen** (VMID/IP gewijzigd in de GUI): hergebruik op naam+tag moet de nieuwe config toepassen, niet stil de oude houden → test in Task 6.
2. **Spoke zonder wan2** (`wan2_intf` leeg): geen ISP-B-reservering, geen ISP-B-link, geen port2-VLAN-afhankelijkheid → test in Task 2 en Task 3.
3. **Agent onbereikbaar** (CT uit, verkeerd token): GUI-tab toont een duidelijke melding i.p.v. een lege pagina; API geeft 502 met tekst → test in Task 8.
4. **Waarden buiten bereik** in `PUT /links` (loss 150, negatieve delay, onbekend IP): agent weigert met 400 en laat de bestaande stand staan → test in Task 4.
5. **Geen internet vanuit de segmenten** (NAT of uplink werkt niet): `pve_wan` meldt `warn` ("geen internet vanuit ISP-A: controleer NAT op wan-emu en de uplink 10.10.106.1") en de uitrol gaat door → test in Task 6.

---

### Task 1: Spike op PVE01 — netem/ifb in een unprivileged CT (handmatig, na akkoord van Sander)

Beslist of de CT `unprivileged: 1` kan blijven. Raakt de PVE-host (modules laden) en maakt een tijdelijke CT: **vooraf akkoord vragen**.

- [ ] **Step 1:** Op PVE01: `modprobe sch_netem ifb` en `/etc/modules-load.d/wan-emu.conf` met `sch_netem` en `ifb`.
- [ ] **Step 2:** Tijdelijke unprivileged CT (Debian 13, VMID vrij, NIC op VLAN 3201) starten; daarin `ip link add ifb0 type ifb`, `tc qdisc add dev eth0 root netem delay 50ms`, `tc qdisc add dev eth0 handle ffff: ingress` + `tc filter add dev eth0 parent ffff: matchall action mirred egress redirect dev ifb0`.
- [ ] **Step 3:** Resultaat vastleggen in de spec (sectie Host-voorwaarde, regel "Open punt" vervangen door de uitkomst). Lukt `ifb` niet: `WANEMU_PRIVILEGED = True` in Task 2 (`ct_params` zet dan `unprivileged: 0`).
- [ ] **Step 4:** Tijdelijke CT verwijderen (na bevestiging); host-module-config laten staan.

### Task 2: Model en pure netwerkfuncties

**Files:**
- Modify: `app/model.py` (`DEFAULTS["wanemu"]`, `_hub`-standaarden, `validate_wanemu`, aanroep in `validate_pve`)
- Create: `app/wanemu/__init__.py`, `app/wanemu/net.py`
- Test: `tests/test_wanemu_net.py`

**Interfaces:**
- Produces:
  - `model.DEFAULTS["wanemu"] = {"vmid": None, "hostname": "wan-emu", "template": "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst", "storage": "local-lvm", "bridge": "vmbr0", "cores": 1, "memory_mb": 512, "disk_gb": 4, "uplink": {"vlan": 106, "ip": "10.10.106.250/24", "gw": "10.10.106.1"}, "segments": {"hub": {"name": "ISP-HUB", "vlan": 3200, "subnet": "100.127.200.0/24", "gw": "100.127.200.1"}, "a": {"name": "ISP-A", "vlan": 3201, "subnet": "100.127.201.0/24", "gw": "100.127.201.1", "res_start": 11, "pool": [100, 199]}, "b": {"name": "ISP-B", "vlan": 3202, "subnet": "100.127.202.0/24", "gw": "100.127.202.1", "res_start": 11, "pool": [100, 199]}}, "agent_port": 8090}`
  - Hub-standaarden: `wan1_ip` `100.127.200.11/24` / `.12/24`, `wan1_gw` `100.127.200.1`.
  - `model.validate_wanemu(t) -> list[str]` (Nederlandse foutteksten).
  - `net.SEG_IFACE = {"hub": "eth1", "a": "eth2", "b": "eth3"}`
  - `net.mac_for(dev_name: str, port_no: int) -> str`
  - `net.reservations(t) -> list[dict]` met keys `name, seg ("a"|"b"), ip, mac`
  - `net.links(t) -> list[dict]` met keys `id ("<dev>-wan<n>"), name, seg, iface, ip`
  - `net.dnsmasq_conf(t) -> str`
  - `net.ct_params(t, vmid: int, ssh_pubkey: str) -> dict` (Proxmox `POST /nodes/{node}/lxc`-body)
  - `net.WANEMU_PRIVILEGED = False` (Task 1 kan dit omzetten)

- [ ] **Step 1: Write the failing tests** in `tests/test_wanemu_net.py`:

```python
t = model.merged({})
m = net.mac_for("site1-1", 1)
assert m == net.mac_for("site1-1", 1) and m != net.mac_for("site1-1", 2) and m.startswith("02:")
assert re.fullmatch(r"02(:[0-9a-f]{2}){5}", m)
r = net.reservations(t)
assert [(x["name"], x["seg"], x["ip"]) for x in r[:2]] == [("site1-1", "a", "100.127.201.11"), ("site1-1", "b", "100.127.202.11")]
assert r[0]["mac"] == net.mac_for("site1-1", 1) and r[1]["mac"] == net.mac_for("site1-1", 2)
L = net.links(t)
assert {"id": "site1-H1-wan1", "name": "site1-H1", "seg": "hub", "iface": "eth1", "ip": "100.127.200.11"} in L
assert len(L) == 7   # 2 hubs + site1-1 (2) + site1-2 (alleen wan1) + site1-3 (2)
# spoke zonder wan2 (Review Focus 2)
t2 = model.merged({}); t2["edges"][0]["wan2_intf"] = ""
assert not any(x["name"] == t2["edges"][0]["name"] and x["seg"] == "b" for x in net.reservations(t2) + net.links(t2))
c = net.dnsmasq_conf(t)
assert "interface=eth2" in c and "interface=eth3" in c and "interface=eth1" not in c
assert "dhcp-range=set:a,100.127.201.100,100.127.201.199,255.255.255.0,12h" in c
assert f"dhcp-host={net.mac_for('site1-1', 1)},100.127.201.11" in c and "dhcp-option=tag:a,option:router,100.127.201.1" in c
assert "dhcp-option=tag:a,option:dns-server,1.1.1.1,8.8.8.8" in c
p = net.ct_params(t, 130, "ssh-ed25519 AAAA test")
assert p["net0"] == "name=eth0,bridge=vmbr0,tag=106,ip=10.10.106.250/24,gw=10.10.106.1"
assert p["net2"] == "name=eth2,bridge=vmbr0,tag=3201,ip=100.127.201.1/24"
assert p["unprivileged"] == 1 and p["hostname"] == "wan-emu" and "fgt-sdwan" in p["tags"]
# validatie
assert model.validate_wanemu(t) == []
bad = model.merged({}); bad["hubs"][0]["wan1_ip"] = "10.10.106.101/24"
assert any("ISP-HUB" in e for e in model.validate_wanemu(bad))
bad = model.merged({}); bad["wanemu"]["segments"]["b"]["vlan"] = 3201
assert any("VLAN" in e for e in model.validate_wanemu(bad))
bad = model.merged({}); bad["wanemu"]["segments"]["b"]["subnet"] = "100.127.201.0/25"
assert any("overlap" in e for e in model.validate_wanemu(bad))
```

- [ ] **Step 2:** `python -W ignore tests/test_wanemu_net.py` → FAIL (`No module named app.wanemu`).
- [ ] **Step 3:** Implementeer. `mac_for`: `02:` + eerste 4 bytes van `sha1(dev_name)` + `port_no` als laatste byte. `validate_wanemu` controleert: hub-wan1 binnen ISP-HUB-subnet en gw == ISP-HUB-gw; VLAN's uniek (ook t.o.v. uplink); subnets overlappen niet onderling of met uplink; uplink-IP/gw geldig en gw in het uplink-subnet; segment-gw binnen subnet; `res_start + aantal spokes - 1 < pool[0]` (anders "te veel spokes voor de reserveringen"). `validate_pve` roept `validate_wanemu` aan (alleen relevant bij Day-0, dat is waar `validate_pve` al draait). Pas `normalize_wans`-gerelateerde tests aan die de oude /29-hubadressen verwachten.
- [ ] **Step 4:** Alle tests draaien (`for f in tests/test_*.py; do python -W ignore $f || break; done`) → alles OK.
- [ ] **Step 5:** Commit `WAN-emulator: model, standaarden en netwerkfuncties`.

### Task 3: FortiGate-NIC's op de ISP-VLAN's met vaste MAC

**Files:**
- Modify: `app/provision_vms.py` (`fgt_nics`, `vm_params`)
- Test: `tests/test_wanemu_net.py` (uitbreiden), `tests/test_pipeline_day0.py` (verwachtingen)

**Interfaces:**
- Consumes: `net.mac_for`, `DEFAULTS["wanemu"]["segments"]`
- Produces: `fgt_nics(dev, cfg, wanemu, is_hub: bool) -> list[{"bridge","vlan","mac"}]`; `vm_params` schrijft `virtio=<mac>,bridge=…,tag=…` als `mac` gezet is.

- [ ] **Step 1: Failing test:**

```python
t = model.merged({}); cfg, w = t["pve"], t["wanemu"]
h = pv.fgt_nics(t["hubs"][0], cfg, w, True)
assert (h[0]["vlan"], h[0]["mac"]) == (3200, net.mac_for("site1-H1", 1)) and h[1]["vlan"] is None
e = pv.fgt_nics(t["edges"][0], cfg, w, False)
assert [(n["vlan"], n["mac"]) for n in e[:2]] == [(3201, net.mac_for("site1-1", 1)), (3202, net.mac_for("site1-1", 2))]
assert pv.vm_params({"role": "fgt", "cores": 1, "memory_mb": 1, "extra_disks": [], "nics": e}, dict(cfg, disk_storage="x"), "v", None)["net0"] == f"virtio={net.mac_for('site1-1', 1)},bridge=vmbr0,tag=3201"
t2 = model.merged({}); t2["edges"][0]["wan2_intf"] = ""
assert pv.fgt_nics(t2["edges"][0], cfg, w, False)[1].get("mac") is None   # Review Focus 2
```

- [ ] **Step 2:** Run → FAIL (signature).
- [ ] **Step 3:** Implementeer: de wan-poorten (hub `wan1_intf`; spoke `wan1_intf`/`wan2_intf`) krijgen bridge `wanemu.bridge`, VLAN van het segment en `mac_for`; overige poorten blijven de per-poort-tabel volgen. `build_specs` geeft `topo["wanemu"]` en de rol door.
- [ ] **Step 4:** Alle tests → OK.
- [ ] **Step 5:** Commit `FortiGate-NIC's: wan-poorten op ISP-HUB/ISP-A/ISP-B met vaste MAC`.

### Task 4: Agent (draait in de CT)

**Files:**
- Create: `app/wanemu/agent.py` (één bestand, stdlib, ook los uitvoerbaar: `python3 agent.py --conf /etc/wan-emu`)
- Test: `tests/test_wanemu_agent.py`

**Interfaces:**
- Produces:
  - `agent.DEFAULT = {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0.0, "rate_kbit": 0, "down": False}`
  - `agent.check(values: dict) -> dict` (genormaliseerd; `ValueError` bij delay 0–5000, jitter 0–1000, loss 0–100, rate 0–10_000_000, onbekende keys)
  - `agent.tc_commands(topology: list[dict], state: dict[str, dict]) -> list[list[str]]` (topology = `net.links`-uitvoer; state per link-id)
  - `agent.nft_ruleset(topology, state) -> str` (bevat altijd `oifname "eth0" ip saddr 100.127.200.0/22 masquerade` — prefix afgeleid uit de segmenten — plus drop-regels voor links met `down`)
  - `agent.App(conf_dir, token, run=subprocess.run)` met `apply()`, `serve(host, port)`; HTTP: `GET /links`, `PUT /links/<id>`, `POST /reset`; 401 zonder juist token, 400 bij `ValueError`/onbekend id, stand ongewijzigd bij fout.

- [ ] **Step 1: Failing tests:** golden-output voor `tc_commands` met één link `site1-1-wan1` (eth2, 100.127.201.11) op `delay 40, jitter 5, loss 1, rate 20000`:

```python
cmds = agent.tc_commands(topo, {"site1-1-wan1": dict(agent.DEFAULT, delay_ms=40, jitter_ms=5, loss_pct=1, rate_kbit=20000)})
s = [" ".join(c) for c in cmds]
assert "tc qdisc replace dev eth2 root handle 1: htb default 9999" in s
assert "tc qdisc replace dev ifb-eth2 root handle 1: htb default 9999" in s
assert any("netem delay 40ms 5ms loss 1%" in x and "dev eth2" in x for x in s)
assert any("match ip dst 100.127.201.11/32" in x and "dev eth2" in x for x in s)
assert any("match ip src 100.127.201.11/32" in x and "dev ifb-eth2" in x for x in s)
assert any("rate 20000kbit" in x for x in s)
assert "ip saddr 100.127.201.11 drop" not in agent.nft_ruleset(topo, {})
assert 'oifname "eth0"' in agent.nft_ruleset(topo, {}) and "masquerade" in agent.nft_ruleset(topo, {})
assert "ip saddr 100.127.201.11 drop" in agent.nft_ruleset(topo, {"site1-1-wan1": dict(agent.DEFAULT, down=True)})
```

  Plus een HTTP-test met `App(tmpdir, "tok", run=fake)` op een vrije poort in een thread: 401 zonder token; `PUT` met `{"loss_pct": 150}` → 400 en `GET` geeft nog de oude stand (Review Focus 4); `PUT` geldig → 200, `links.json` bijgewerkt, `fake` kreeg `tc`- en `nft -f`-aanroepen; nieuwe `App` op dezelfde map leest de stand terug (herstart).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer. Per segment-interface één htb-root (default-klasse 9999 onbeperkt), per link klasse `1:<n>` (n = index+10) met `netem` als leaf en een `u32`-filter op dst; ingress `ffff:` met `matchall … mirred egress redirect dev ifb-<iface>` en dezelfde opbouw op `ifb-<iface>` met src-filters. Rate 0 → htb `rate 10gbit`. `apply()` verwijdert eerst de roots (`tc qdisc del … 2>/dev/null` = negeer fouten) en bouwt alles opnieuw; nft via een eigen tabel `inet wanemu` die volledig vervangen wordt. `ip link add ifb-<iface> type ifb` alleen als hij nog niet bestaat.
- [ ] **Step 4:** Alle tests → OK.
- [ ] **Step 5:** Commit `WAN-emulator: agent (tc/netem/ifb, nftables, HTTP met token)`.

### Task 5: Proxmox-API voor LXC

**Files:**
- Modify: `app/pve.py`, `tests/fake_proxmox.py`
- Test: `tests/test_day0_units.py` (uitbreiden)

**Interfaces:**
- Produces: `Pve.find_cts(tag) -> list[dict]`, `Pve.create_ct(node, vmid, params) -> upid`, `Pve.set_ct_config(node, vmid, params)`, `Pve.start_ct(node, vmid) -> upid`, `Pve.stop_ct(node, vmid) -> upid`, `Pve.delete_ct(node, vmid) -> upid`, `Pve.ct_status(node, vmid) -> str`.

- [ ] **Step 1: Failing test:** tegen de fake: `create_ct` → POST `/nodes/n/lxc` met `vmid`, `ostemplate`, `net0…net3`, `ssh-public-keys`; `find_cts("fgt-sdwan")` vindt hem (via `/cluster/resources?type=vm`, `type == "lxc"`); `set_ct_config` → PUT `/nodes/n/lxc/<id>/config`; `delete_ct` → DELETE met `purge=1`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer (zelfde `_req`/`wait_task`-patroon als de VM-methodes; `find_vms` laat LXC's buiten beschouwing zodat VM-hergebruik op naam niet botst).
- [ ] **Step 4:** Alle tests → OK.
- [ ] **Step 5:** Commit `Proxmox-client: LXC aanmaken, configureren, starten, verwijderen`.

### Task 6: Inrichting en pipeline-stap `pve_wan`

**Files:**
- Create: `app/wanemu/setup.py`
- Modify: `app/fmg.py` (stap tussen templates en `pve_fgt`), `app/main.py` (`STEPS`, secrets naar boot), `app/db.py` (`get_secret(conn, name, create=False)` — generiek, zelfde 0600-db als `get_psk`)
- Test: `tests/test_wanemu_pipeline.py`, bestaande `tests/test_pipeline_day0.py`, `tests/test_scope.py`

**Interfaces:**
- Consumes: Task 2 (`ct_params`, `dnsmasq_conf`, `links`), Task 4 (`agent.py` als bestand), Task 5 (Pve-LXC).
- Produces:
  - `setup.deploy_wanemu(pve, topo, secrets: {"ssh_key": str (PEM), "agent_token": str}, emit, step="pve_wan", connector=None) -> {"vmid": int, "created": bool, "internet": bool}`
  - `setup.provision(ssh, topo, agent_token, emit)` (idempotent; schrijft `/etc/dnsmasq.d/wan-emu.conf`, `/etc/wan-emu/topology.json`, `/opt/wan-emu/agent.py`, `/etc/systemd/system/wan-emu-agent.service`, sysctl, en herstart dnsmasq + agent)
  - `setup.internet_check(ssh) -> bool` (`ping -c2 -W2 -I 100.127.201.1 1.1.1.1`)
  - db-secrets `wanemu_ssh_key`, `wanemu_agent_token`
  - main `STEPS`-regel `("pve_wan", "WAN-emulator", "LXC wan-emu: ISP-HUB/ISP-A/ISP-B, DHCP, verkeersmanipulatie")`

- [ ] **Step 1: Failing tests** (fake Proxmox + fake SSH-connector die commando's/bestanden vastlegt, zoals `fake_fortios` voor bootstrap):
  - Day-0-uitrol: stap-volgorde bevat `templates < pve_wan < pve_fgt`; CT aangemaakt met de params uit Task 2; dnsmasq-config en agent geüpload; `systemctl restart wan-emu-agent` aangeroepen.
  - Tweede uitrol: CT op naam+tag hergebruikt (`created False`), `set_ct_config` met de **nieuwe** params (uplink-IP gewijzigd in de topologie → nieuwe `net0`) en opnieuw ingericht (Review Focus 1).
  - `internet_check` faalt → event `warn` met tekst bevat "geen internet vanuit ISP-A"; uitrol gaat door naar `pve_fgt` (Review Focus 5).
  - Fout tijdens inrichting van een **nieuwe** CT → CT verwijderd, uitrol faalt; bij een **hergebruikte** CT → niet verwijderd.
  - Methode `ssh`/`none` en scopes `fmg`/`tpl`/`dev`: `pve_wan` = `skipped`, geen LXC-aanroepen.
  - Fake SSH laat `ip link add ifb-test` falen → stap faalt met de tekst over `sch_netem`/`ifb`.
  - Dry-run: alleen `[dry-run]`-regels; geen token of PEM in enig event.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer. SSH als root met de ed25519-sleutel uit de db (eerste keer gegenereerd met paramiko; publieke sleutel in `ssh-public-keys`). Wacht na start tot TCP 22 op het uplink-IP open is (bestaande `wait_tcp`). Pakketten: `apt-get update && apt-get install -y dnsmasq nftables iproute2 python3` (alleen als `dpkg -s` ze mist). Agent-token alleen via een bestand met mode 0600 (`/etc/wan-emu/token`), nooit op de commandline. Controleer netem/ifb vóór het starten van de agent (`ip link add ifb-test type ifb` en `tc qdisc replace dev eth1 root netem delay 0ms`, daarna opruimen); faalt dat → stap `error` met "laad sch_netem en ifb op de PVE-host (/etc/modules-load.d/wan-emu.conf)".
- [ ] **Step 4:** Alle tests → OK.
- [ ] **Step 5:** Commit `Pipeline: stap pve_wan (LXC wan-emu uitrollen en inrichten)`.

### Task 7: Instellingen in de GUI

**Files:**
- Modify: `static/index.html`, `static/app.js`, `static/style.css`, `app/csvio.py` (volledige export bevat `wanemu`, zonder secrets)
- Test: `tests/test_ui_static.py`, `tests/test_csv.py`

**Interfaces:**
- Consumes: `DEFAULTS["wanemu"]` via `/api/defaults`; validatie via bestaande `/api/validate`.

- [ ] **Step 1: Failing test:** `test_ui_static` vindt de elementen `#wanemu-section`, `#wan-uplink-ip`, `#wan-seg-hub-vlan`, `#wan-seg-a-subnet`, `#wan-seg-b-gw`; de sectie heeft de class `only-vm` (zichtbaar bij Day-0, net als de andere Proxmox-velden) en er is **geen** aan/uit-checkbox. `test_csv`: volledige export/import round-trip van `wanemu`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer de sectie (container: VMID, template, storage, bridge; uplink: VLAN, IP, gateway; per segment: VLAN, subnet, gateway; ISP-A/B ook reservering-start en pool). Velden gevuld uit de standaarden; hub-wan1-velden tonen de nieuwe standaarden.
- [ ] **Step 4:** Alle tests → OK.
- [ ] **Step 5:** Commit `GUI: instellingen WAN-emulatie`.

### Task 8: API-proxy en GUI-tab "WAN-emulatie"

**Files:**
- Create: `app/wanemu/client.py`
- Modify: `app/main.py` (routes), `static/index.html`, `static/app.js`, `static/style.css`
- Test: `tests/test_wanemu_api.py`, `tests/test_ui_static.py`

**Interfaces:**
- Consumes: Task 4-HTTP-API, db-secret `wanemu_agent_token`, `wanemu.uplink.ip`, `agent_port`.
- Produces: `client.Agent(base_url, token, timeout=5)` met `links()`, `put(link_id, values)`, `reset()`, fout `AgentError(msg)`; routes `GET /api/wanemu/links` (topologie-links samengevoegd met de stand), `PUT /api/wanemu/links/{id}`, `POST /api/wanemu/preset/{name}`, `POST /api/wanemu/reset`; `client.PRESETS: dict[str, dict]`.
- Presets (waarden per link, in beide richtingen): `neutraal` 0/0/0/0; `glasvezel` delay 2, jitter 0, loss 0, rate 1000000; `dsl` 15/3/0.1/50000; `slechte-4g` 60/25/3/10000; `satelliet` 300/20/0.5/20000; `trans-atlantisch` 45/2/0/0. Storing-presets: `isp-a-storing` (alle `seg a` down), `isp-b-storing` (alle `seg b` down), `hub1-weg` (link `<hubs[0].name>-wan1` down). Een preset zonder linkkeuze geldt voor alle links; met `?link=<id>` alleen voor die link.

- [ ] **Step 1: Failing tests:** met de agent uit Task 4 op een lokale poort (`run=fake`): `GET /api/wanemu/links` geeft 7 links met naam/IP/seg en stand; `PUT` geldig → 200 en de agent heeft de waarde; `PUT` loss 150 → 400 met de tekst van de agent; preset `isp-a-storing` zet precies de 3 ISP-A-links op `down`; agent gestopt → `GET` 502 met tekst "WAN-emulator niet bereikbaar op 10.10.106.250:8090" (Review Focus 3); token komt in geen enkele response voor. `test_ui_static`: tab `#tab-wanemu` en container `#wanemu-links` bestaan.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer. Tab: drie kolommen (ISP-HUB, ISP-A, ISP-B) met per link een kaart (naam, IP, vier schuifjes, "link down", "toepassen"); RTT-indicatie = 2 × delay; presetknoppen bovenaan; foutmelding van de API zichtbaar boven de kaarten; verversen elke 10 s zolang de tab open is.
- [ ] **Step 4:** Alle tests → OK.
- [ ] **Step 5:** Commit `GUI-tab WAN-emulatie met presets en proxy naar de agent`.

### Task 9: Documentatie, PR, CT 100, echte test

- [ ] **Step 1:** README: sectie WAN-emulatie (topologie, NAT op eth0, host-modules `sch_netem`/`ifb`). Hermes-skill `fortimanager-sdwan-deployment/references/proxmox-day0.md`: pitfalls uit Task 1 en de echte test.
- [ ] **Step 2:** Branch `claude/wan-emulator`, PR, merge na akkoord; CT 100 updaten als `/api/jobs` geen lopende job heeft.
- [ ] **Step 3:** Echte uitrol (na akkoord, VM's 111–115 eerst opruimen): controleren dat hubs `100.127.200.11/.12` hebben, spokes DHCP-adressen `.11+n` in ISP-A/ISP-B, alle tunnels (WAN1 én WAN2) en BGP up, en dat een preset `slechte-4g` op één spoke zichtbaar is in `diagnose sys sdwan health-check` (latency ±120 ms naar Lo-HC).
