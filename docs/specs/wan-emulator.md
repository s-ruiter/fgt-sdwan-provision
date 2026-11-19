# WAN-emulator (ISP-HUB / ISP-A / ISP-B) — ontwerp

## Doel

Hubs en spokes zitten niet meer samen in het lab-L2 (10.10.106.0/24) maar achter een gesimuleerde provider:
hubs in **ISP-HUB**, spoke-WAN1 in **ISP-A**, spoke-WAN2 in **ISP-B**. Eén LXC-container (`wan-emu`) routeert
tussen de segmenten, is DHCP-server voor ISP-A/ISP-B en manipuleert per link het verkeer (latency, jitter, loss,
bandbreedte, link down) met `tc`/`nftables`. De deploy-tool rolt de container mee uit en krijgt een GUI-tab om de
links aan te sturen.

De WAN-emulatie staat **altijd aan** zodra de tool de FortiGates op Proxmox aanmaakt (methode Day-0). Er is geen
aan/uit-schakelaar.

## Topologie

```
                 lab 10.10.106.0/24 (VLAN 106), gw 10.10.106.1
                              │ eth0 10.10.106.250/24
                 ┌────────────┴────────────┐
                 │  LXC wan-emu (Debian 13) │  routing, NAT naar eth0, dnsmasq, tc, nftables, agent
                 └──┬─────────┬─────────┬──┘
     eth1 .200.1/24 │ eth2 .201.1/24    │ eth3 .202.1/24
    ISP-HUB (3200)   ISP-A (3201)        ISP-B (3202)
    hubs port1       spokes port1        spokes port2
    vast .11, .12    DHCP                DHCP
```

| Segment | VLAN | Subnet | Gateway | Toewijzing |
|---|---|---|---|---|
| uplink | 106 | 10.10.106.0/24 | 10.10.106.1 | container 10.10.106.250 |
| ISP-HUB | 3200 | 100.127.200.0/24 | 100.127.200.1 | site1-H1 .11, site1-H2 .12 (vast, Day-0) |
| ISP-A | 3201 | 100.127.201.0/24 | 100.127.201.1 | spoke-WAN1: reservering per MAC vanaf .11, pool .100–.199 |
| ISP-B | 3202 | 100.127.202.0/24 | 100.127.202.1 | spoke-WAN2: reservering per MAC vanaf .11, pool .100–.199 |

Alle NICs op `vmbr0` met een VLAN-tag (vmbr0 is niet VLAN-aware; Proxmox maakt per tag `vmbr0vNNNN`).

### NAT naar het lab

De container routeert tussen de segmenten zonder NAT (spokes zien de echte hub-adressen). Verkeer dat via eth0 naar
het lab of internet gaat, krijgt **masquerade** naar 10.10.106.250. Op de lab-gateway hoeft dus niets te veranderen;
FortiManager ziet alle FortiGates als 10.10.106.250 (fgfm wordt door de FortiGate opgezet, dat werkt door NAT).
De tool controleert internet vanuit de segmenten (ping met bron 100.127.201.1 naar 1.1.1.1) en waarschuwt als dat
niet lukt.

## Container

- Unprivileged LXC, Debian 13 (`local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst`, instelbaar), 1 vCPU,
  512 MB, 4 GB disk, tag `fgt-sdwan`, hostname `wan-emu`. VMID instelbaar, standaard de eerstvolgende vrije.
- Aangemaakt via de Proxmox-API (`POST /nodes/{node}/lxc`) met een SSH-publieke sleutel die de tool genereert
  (privésleutel in de tool-db, 0600). Proxmox-API kent geen exec voor LXC; de tool configureert de container via SSH
  (paramiko) op 10.10.106.250.
- Inrichting (idempotent, opnieuw draaien mag): `net.ipv4.ip_forward=1`, rp_filter uit, pakketten `dnsmasq
  nftables iproute2 python3`, dnsmasq-config per segment, nftables-basis, agent + systemd-unit.
- Bestaat de container al (naam + tag), dan wordt hij hergebruikt en opnieuw ingericht (bijv. nieuwe reserveringen).
  Bij een mislukte uitrol verwijdert de tool alleen een container die hij in deze run zelf heeft aangemaakt.

### Host-voorwaarde

`sch_netem` en `ifb` moeten op PVE01 geladen zijn (`/etc/modules-load.d/wan-emu.conf`). De tool controleert dit en
meldt het; hij laadt geen modules op de host.

Getest op PVE01 (PVE 9, Debian 13-template) in een **unprivileged** CT: `ifb`-devices aanmaken, htb, netem, ingress
met matchall + mirred, u32-filters en nftables met NAT werken allemaal; de container blijft dus unprivileged. Verder
gevonden: zonder `features: nesting=1` falen onder systemd 257 de mounts van /tmp, /run/lock en mqueue (de tool zet
nesting aan); `sysctl --system` faalt in de CT, `sysctl -p` van het eigen bestand werkt; bij het opstarten bestaat het
uplink-IP soms nog niet, dus de agent wacht daarop; na een herstart staan forwarding, de linkstand en de NAT weer goed.

## DHCP

- dnsmasq op eth2 (ISP-A) en eth3 (ISP-B): router = .1 van het segment, DNS = `dns_primary`/`dns_secondary` uit de
  topologie, lease 12 h.
- Reservering per spoke: spoke n (volgorde in de topologie, 0-based) krijgt .11+n op ISP-A (port1) en ISP-B (port2).
  Maximaal 89 spokes (.11–.99); de validatie bewaakt dat.
- De tool geeft de FortiGate-NICs **vaste MAC-adressen**: locally administered `02:fs:xx:xx:xx:pp`, afgeleid van
  devicenaam + poortnummer (stabiel bij een nieuwe uitrol). `net{i}: virtio=<mac>,bridge=vmbr0,tag=<vlan>`.

## Verkeersmanipulatie

Per **link** = (segment, FortiGate-IP): `delay_ms`, `jitter_ms`, `loss_pct`, `rate_kbit` (0 = onbeperkt), `down`.

- Richting naar de FortiGate: op de segment-interface een `htb`-root, per link een klasse met `netem` eronder, filter
  op `ip dst`.
- Richting van de FortiGate: ingress van de segment-interface via `mirred` naar `ifb-<seg>`, daar dezelfde opbouw met
  filter op `ip src`.
- De waarden gelden in beide richtingen (een opgegeven delay telt dus twee keer in de RTT; de GUI toont de RTT).
- `down`: nftables-regel in de forward-chain die verkeer van/naar dat IP laat vallen.
- Hubs zijn ook links (ISP-HUB), dus "hub weg" kan ook.

### Agent

Klein Python-programma (stdlib, geen extra pakketten) in de container, systemd-service op `10.10.106.250:8090`.
- Auth: bearer-token, door de tool gegenereerd en in de tool-db bewaard; alleen bereikbaar via eth0.
- `GET /links` → huidige stand; `PUT /links/<ip>` → waarden zetten; `POST /reset` → alles neutraal.
- Bewaart de stand in `/etc/wan-emu/links.json` en past die bij het starten opnieuw toe.
- De lijst van links (IP, segment, naam) schrijft de tool tijdens de inrichting in `/etc/wan-emu/topology.json`.

## Deploy-tool

### Instellingen (sectie "WAN-emulatie", altijd zichtbaar bij Day-0)

Container: VMID (leeg = automatisch), template, storage, bridge (`vmbr0`), uplink-VLAN (106), uplink-IP
(10.10.106.250/24), uplink-gateway (10.10.106.1). Segmenten ISP-HUB / ISP-A / ISP-B: VLAN, subnet, gateway; voor
ISP-A/B ook reservering-start (.11) en pool (.100–.199). Alle waarden vooraf ingevuld zoals in de tabel hierboven.

### Gevolgen voor bestaande onderdelen

- **Hub-standaarden**: wan1 100.127.200.11/24 en .12/24, gateway 100.127.200.1.
- **Validatie**: hub-wan1 binnen ISP-HUB en gateway = ISP-HUB-gateway; VLAN's uniek; subnets overlappen niet en niet
  met de uplink; uplink-IP binnen zijn subnet; aantal spokes past in de reserveringen.
- **NIC's**: hub port1 → VLAN ISP-HUB; spoke port1 → ISP-A; spoke port2 → ISP-B. Andere poorten (LAN) blijven de
  per-poort-tabel volgen. De per-poort-instelling voor port1/port2 vervalt voor FortiGates.
- **Pipeline**: nieuwe stap `pve_wan` direct vóór `pve_fgt`; alleen bij FortiGate-methode Day-0, anders
  "overgeslagen". Volgorde: … templates, **pve_wan**, pve_fgt, bootstrap_fgt, install.
- **Methode SSH** (bestaande FortiGates via `initial_ip`) blijft zoals hij is, zonder WAN-emulator.
- **Scopes** fmg/tpl/dev: geen container; de hub-adressen uit ISP-HUB gelden wel voor de templates en metadata.
- **Dry-run**: alles gelogd, niets naar Proxmox of de container.

### GUI-tab "WAN-emulatie"

- Schema met de drie segmenten en per FortiGate de links (hub: 1, spoke: 2) met naam en IP.
- Per link: schuifjes latency (0–1000 ms), jitter (0–200 ms), loss (0–100 %), bandbreedte (0 = onbeperkt … 1 Gbit),
  knop "link down", en de huidige stand (gelezen van de agent).
- Presets: Neutraal, Glasvezel, DSL, Slechte 4G, Satelliet, Trans-Atlantisch; plus "ISP-A storing", "ISP-B storing",
  "Hub 1 weg" (zet meerdere links tegelijk).
- De tool proxyt naar de agent: `GET/PUT /api/wanemu/links`, `POST /api/wanemu/reset`. Het token blijft in de tool.

## Testen

- Unit: MAC-afleiding, reserveringen, dnsmasq-config, tc/nft-opdrachten uit een linkstand (golden output),
  validatie, NIC/VLAN-toewijzing per rol, Proxmox-LXC-params.
- Pipeline: fake Proxmox + fake SSH; stap `pve_wan` vóór `pve_fgt`, rollback alleen van een zelf aangemaakte CT,
  dry-run, overslaan bij methode ssh/none.
- Agent: draait lokaal met een nep-`tc`/`nft` (opdrachten worden vastgelegd), persistentie en herstart.
- Echt: één keer op PVE01 met een test-CT (na akkoord), daarna een volledige uitrol.

## Buiten scope

Meerdere hub-sites/regio's, IPv6, NAT/CGNAT-simulatie (kan later per segment), BGP in de container.
