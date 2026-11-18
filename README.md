# fgt-sdwan-provision

_UI: dezelfde look & feel als [Spawnpad](https://github.com/s-ruiter/Spawnpad) (Geist, rode accenten, kaarten; lettertype onder SIL OFL, zie `static/fonts/LICENSE.TXT`)._

Deploy tool voor een Fortinet **SD-WAN/ADVPN multi-VRF** topologie via **FortiManager 8.0.x**:
2 hubs + 3 branches in één regio, ontworpen als LXC op Proxmox met een web-UI.

- Config komt uit Fortinet's [Jinja Orchestrator](https://github.com/fortinet-solutions-cse/sdwan-advpn-reference) (release 7.6, `dynamic-bgp-on-lo`, multi-VRF), **offline gerenderd** in deze tool. Er worden **geen Jinja-templates in FortiManager** gebruikt.
- De Orchestrator wordt *symbolisch* gerenderd: per-device waarden worden FortiManager-metadata-variabelen (`$(loopback)`, `$(lan_ip_netmask)` ...). Daaruit maakt de tool **native FortiManager-objecten**: CLI-templates (underlay/overlay/routing/inter-VRF) + CLI-template-group, system template (DNS), native SD-WAN-template, static-route-template, policy package, interfaces/adressen/IP-pools, device group, template group en device blueprint. Eén set per hub (HUB1, HUB2) en per groep branches met identieke config (SPOKES-n; zelfde poortnamen vereist voor de native SD-WAN-template).
- ADOM-naam en serienummers geeft de gebruiker op.
- **Vaste waarden** (niet instelbaar, afgedwongen in `model.merged`): platform FortiGate-VM64, DNS 1.1.1.1 en 8.8.8.8, wan1 allowaccess `ping https ssh fgfm`, regio West, BGP AS 65001, loopback-summary 10.200.1.0/24 (regio) en 10.200.0.0/14. De **IPsec-PSK** genereert de tool zelf (willekeurig, 45 tekens), per ADOM bewaard in de SQLite-database (0600) zodat een herhaalde uitrol de sleutel niet wijzigt; previews tonen een placeholder. Let op: de PSK staat leesbaar in de CLI-templates in FortiManager.
- **CSV import/export** (kaart *Topologie*): *CSV downloaden* geeft de **devices** (`[devices]`, incl. kolom `fortiflex_token`; LAN-kolommen volgens de gekozen modus). *Alles exporteren naar CSV* (onderaan de branches) geeft ook `[algemeen]` (ADOM, FortiManager incl. `fmgr.fortiflex_token`), `[vrf]` en `[proxmox]` (servernaam, node, storages, VM-groottes, poort-bridges). Bij importeren neemt de tool standaard alleen de devices (en hun tokens) over; bevat het bestand ook instellingen, dan vraagt hij of die ook overgenomen moeten worden. **De FortiFlex-tokens staan in het bestand: behandel het als geheim.** Admin-wachtwoorden en het Proxmox-secret staan er nooit in. Bestanden met de oude kolomnamen (`isp1_intf`, `isp2_intf`, `isp1_ip`, `isp2_ip`) blijven importeerbaar; MPLS-kolommen worden genegeerd.
- **Multi-VRF of niet** is een keuze in de UI (kaart *VRF's*): aan = PE-VRF 1 + klant-VRF's met leak (standaard 11 en 12); uit = klassiek single-VRF met één LAN per device (geen PE-VRF, leak of `vpn-id-ipip`). Beide varianten zijn tegen een echte FortiManager 7.6.8 aangemaakt en teruggelezen.

## Installeren (op de Proxmox-host, als root)

**Vanuit een checkout (werkt ook als de repo privé is):** de code wordt lokaal naar de container gekopieerd.

```bash
gh repo clone s-ruiter/fgt-sdwan-provision && scp -r fgt-sdwan-provision root@<pve-ip>:/root/
ssh root@<pve-ip> bash /root/fgt-sdwan-provision/ct/deploy.sh
```

**One-liner (repo is publiek):**

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/deploy.sh)"
```

**Privé-repo zonder checkout:** `GITHUB_TOKEN=ghp_xxx bash -c "$(curl -fsSL -H "Authorization: token $GITHUB_TOKEN" https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/deploy.sh)"` (token met leesrechten op de repo).

Het script toont eerst een samenvatting (CTID, storage, netwerk) en vraagt om bevestiging (`--yes` of `YES=1` slaat dat over). Storages worden automatisch gekozen (eerste actieve met `rootdir`/`vztmpl`). Overrides via environment: `CTID`, `CT_HOSTNAME`, `STORAGE`, `TEMPLATE_STORAGE`, `BRIDGE`, `VLAN`, `CT_IP` (`dhcp` of `a.b.c.d/nn`), `CT_GW`, `CT_DNS`, `CORES`, `MEMORY`, `DISK_GB` (standaard 12).
Daarna: `http://<ct-ip>:8000`.

**Updaten** (data en opgeslagen servers blijven staan): `UPDATE=1 CTID=<id> bash /root/fgt-sdwan-provision/ct/deploy.sh` (met een verse checkout), of voor een GitHub-installatie `pct exec <id> -- bash /opt/fgt-sdwan-provision/ct/install.sh`.

De container heeft uitgaand internet nodig (Debian-mirrors, PyPI, github.com voor Fortinet's Orchestrator) en moet de Proxmox-API (:8006) en de FortiManager kunnen bereiken. De Orchestrator-repo heeft geen licentiebestand en wordt daarom bij de installatie geclonet (vastgepind op een commit) in plaats van hier meegeleverd.

## Uitrolstappen (zichtbaar in de UI)

Bovenaan de pagina *Uitrol* kies je **wat** je uitrolt:

- **Alles** (standaard): FortiManager en FortiGates, alle stappen hieronder.
- **Alleen FortiManager**: stappen 1-5 en 9 (VM, licentie, FortiManager-instellingen, ADOM, templates voor hubs en spokes). Geen model devices, geen FortiGate-VM's en geen install. Device groups blijven leeg en metadata-variabelen/IP-pools krijgen nog geen waarde per device. Namen, loopbacks, LAN's en hub-adressen zijn nodig voor de templates; serienummers, wan1-gateways, FortiGate-tokens en -wachtwoord niet.
- **Alleen FortiGates**: tegen een bestaande, bereikbare FortiManager (vast IP + admin-wachtwoord uit de tool). Geen FortiManager-VM/ZTP en geen FMG-token of -image nodig; ADOM wordt hergebruikt, model devices toegevoegd (bestaande hergebruikt), de templates opnieuw gezet incl. de waarden per device (set/replace/update, dus handmatige wijzigingen aan deze tool-objecten worden overschreven), daarna FortiGate-VM's en install. Typisch na *Alleen FortiManager*.
- **Alleen model devices**: naar een bestaande, bereikbare FortiManager (IP, admin-gebruiker en wachtwoord): preflight, ADOM, model devices (met serienummer) en templates incl. de waarden per device (metadata-variabelen, device groups). Daarna per device een **Quick install** (device settings) en per rol een **Re-install policy** (policy package), naar de device-database; de tool wacht niet tot devices online zijn en een mislukte install is een waarschuwing per device/package. Geen koppeling naar echte FortiGates: geen Proxmox, geen VM's; de FortiManager-instellingen blijven ongemoeid. Serienummers en hub-gateways zijn nodig, FortiGate-tokens en -wachtwoord niet. Echte FortiGates kunnen zich later op hun serienummer melden, of je rolt ze uit met *Alleen FortiGates*.
- **Alleen templates**: naar een bestaande, bereikbare FortiManager (IP, admin-gebruiker en wachtwoord): alleen stap 3, 5 en 9 (preflight, ADOM, templates voor hubs en spokes, zonder model devices). Geen Proxmox, geen VM's, en de FortiManager-instellingen (timers, tijdzone, ADOM-modus, FortiAnalyzer) blijven ongemoeid. Hostnaam, gateway en poort van de FortiManager zijn niet nodig; serienummers en wan1-gateways ook niet. Daarna kun je de FortiGates uitrollen met *Alleen FortiGates*.

De API neemt dit als `scope` (`all`, `fmg`, `fgt`, `dev`, `tpl`) in `POST /api/deploy`; de keuze wint van de methode per product.

Per product (FortiManager / FortiGates) kies je een **methode**: `day0` (de tool maakt de VM op Proxmox, cloud-init), `ssh` (bestaande VM, bootstrap over SSH) of `none`.

1. **FortiManager-VM** *(day0)* – image naar Proxmox, VM + datadisk + Day-0 config-drive ISO, starten
2. **FortiManager ZTP** – day0: wachten op het vaste IP (licentie + reboot) · ssh: vast IP, route, DNS en `execute vm-license`
3. **Preflight** – login + versiecheck (8.0.x); daarna wordt de FMG-ISO losgekoppeld en verwijderd
4. **FortiManager-instellingen** – uitlogtimers (CLI, GUI, API, SSO) 180 min, tijdzone Europe/Amsterdam (code 28), ADOMs aan in advanced mode, FortiAnalyzer-features aan. Alleen wat afwijkt wordt gezet; FortiAnalyzer-features aanzetten herstart de FortiManager, de tool wacht daarop. Overgeslagen bij *Alleen FortiGates*.
5. **ADOM** – aanmaken (bestaand: hergebruiken)
6. **Model devices** – met de opgegeven serienummers. FortiManager 7.4.7+ weigert standaard VM-devices ("VM device is not allowed to add"); de tool zet daarom vlak ervoor `fgfm-allow-vm enable` in `system global` (ook op een bestaande FortiManager). FortiManager maakt bij een FortiGate-VM model device alleen `port1` aan; de tool voegt daarna `port2`-`port6` (of tot de hoogste gebruikte poort) toe in de device-database, ook bij devices die al bestonden.
7. **FortiGate-VM's** *(day0)* – per FortiGate een VM + ISO (hostname, admin-wachtwoord, wan1 statisch (hub) of DHCP (branch), DNS, `central-management`, FortiFlex-token), starten
8. **FortiGate ZTP** – day0: wachten tot ze gelicenseerd online zijn in FMG, daarna ISO's opruimen · ssh: zie hieronder
9. **Templates** – variabelen, system/SD-WAN/CLI-templates, policy packages, template groups, blueprints (alleen set/replace/update: herhaalbaar)
10. **Install** – policy package per rol op de online devices (offline = waarschuwing, geen fout)

## Images uploaden + Day-0 (cloud-init)

- Download bij FortiCloud (*Support > Firmware Downloads*) het `FGT_VM64_KVM-v…out.kvm.zip` en `FMG_VM64_KVM-v…out.kvm.zip` en upload ze in de UI (sectie *Proxmox & images*). De container pakt de qcow2 uit, controleert hem, toont versie en **sha256** (vergelijk met de portal) en bewaart hem in `/var/lib/fgt-sdwan/images` (LXC-disk standaard 12 GB).
- De tool praat uitsluitend met de **Proxmox-API (token, PVE 9+)**: upload naar een storage met content-type *Import*, VM aanmaken met `import-from`, ISO uploaden naar een ISO-storage. Geen SSH naar Proxmox.
- **Proxmox-gegevens als dropdowns:** na het kiezen van een server haalt de tool node, disk-/import-/ISO-storages (alleen actieve, per node) en bridges rechtstreeks uit Proxmox op; ook de bridge per poort en de FMG-bridge zijn dropdowns. Eerder gekozen waarden blijven staan.
- **Wachtwoorden:** per product één veld (FortiManager, FortiGates) dat voor admin én SSH geldt. Bij een factory-apparaat wordt dit wachtwoord ingesteld; heeft het apparaat het al, dan wordt het gebruikt om in te loggen.
- **Servers (menu *Servers*):** Proxmox-servers voeg je één keer toe (naam, URL, token-ID, token-secret) en kies je daarna bij elke uitrol uit een lijst. De **token-secret wordt bewaard** in `/var/lib/fgt-sdwan/fgt-sdwan.db` (SQLite, rechten 0600, map 0700) en nooit door de API teruggegeven; bij bewerken laat je hem leeg om hem te behouden (wijzig je URL of token-ID, dan moet hij opnieuw). Per server onthoudt de tool ook je laatste keuzes (node, storages, bridge per poort, VM-grootte, licentiemodus) en vult die de volgende keer vanzelf in; de laatst gebruikte server wordt voorgeselecteerd. De database blijft bij updates (`install.sh`) en herstarts staan; maak er een back-up van als je hem wilt bewaren.
- **Let op:** de tool heeft geen login. Wie het web-adres kan bereiken kan met de opgeslagen token VM's aanmaken op die Proxmox. Draai hem alleen op een vertrouwd netwerk en geef de token alleen de rechten die nodig zijn.
- Per VM bouwt de tool een config-drive ISO (label `config-2`, `openstack/latest/user_data`). De licentie wordt toegepast met `execute vm-license <token>` als **laatste CLI-regel** (FortiFlex, `cli`; dezelfde methode als Spawnpad). De UI biedt geen andere licentiemodus aan; de MIME-variant (`config.txt` + `license.txt`) zit alleen nog in de backend en wordt niet gebruikt.
- NIC's: elke FortiGate-VM krijgt minstens 6 virtio-NIC's (port1-6; meer als een device een hogere `portN` gebruikt); bridge/VLAN per poort in de UI. Dit past bij de FortiManager-variabele `vm_interface_number`, die de tool op 6 zet. FMG: 1 NIC + datadisk.
- Metadata-variabelen gelden voor de hele ADOM: elk device ziet de hele lijst. Een variabele die een device niet gebruikt, blijft voor dat device leeg (bv. `wan1_ip`/`wan1_gw` bij branches, `wan2_intf` bij hubs).
- **ISO opruimen:** de ISO bevat admin-wachtwoord en (eenmalig) token. Zodra de VM gelicenseerd/online is, wordt de ISO geeject (`ide2: none`) en van de storage verwijderd.
- Idempotent op VM-naam (tag `fgt-sdwan`): een tweede run hergebruikt bestaande VM's. Faalt het aanmaken, dan worden de VM's, imports en ISO's van die stap weer verwijderd.
- Proxmox-token: minimaal o.a. `VM.Allocate`, `VM.Config.*`, `VM.PowerMgmt`, `VM.Audit`, `Datastore.AllocateSpace`, `Datastore.AllocateTemplate`, `Datastore.Audit`, `Sys.Audit`, `SDN.Use` op de gebruikte bridges (nog niet tegen echte PVE getest; bij 403 toont de uitrol de Proxmox-melding).

## ZTP / FortiFlex (methode `ssh`, voor bestaande VM's)

- **Voortgang blijft bewaard**: uitrollen (stappen + log, zonder geheimen) staan in de SQLite-database. De URL is `#/progress/<id>`, dus verversen, de link delen of de service herstarten verliest niets. Een uitrol die tijdens een herstart liep wordt als *onderbroken* gemarkeerd. De knop *Voortgang* in de balk opent de laatste uitrol; op de pagina Uitrol staat een link naar de laatste.
- **API-login van de tool**: de tool logt in via de FortiManager JSON-RPC-API met de admin-gebruiker en het wachtwoord uit de tool (een sessielogin, geen API-token). Een nieuwe FortiManager heeft voor `admin` `rpc-permit none` en weigert dat met `-11 No permission for the resource`; de Day-0 zet daarom `set rpc-permit read-write` op `admin` (op een FortiManager 7.6.8 gecontroleerd: `admin` = none, een API-user = read-write). De SSH-bootstrap doet hetzelfde voor de ingevulde admin-gebruiker. Met methode `none` moet je dit zelf doen: `config system admin user` / `edit admin` / `set rpc-permit read-write`.
- **FortiManager Day-0**: `port1` wordt expliciet op `set mode static` gezet vóór `set ip` (een nieuwe FortiManager-VM kan op DHCP staan en negeert dan alleen `set ip`; bij een echte uitrol kreeg de FMG zo een DHCP-adres in plaats van het opgegeven vaste IP). Of `mode static` door een FortiManager 8.0-build wordt geaccepteerd is nog niet op een echte 8.0 gezien.
- De FortiManager heeft altijd een vast IP (`a.b.c.d/nn` + gateway). **WAN-ontwerp:** een hub heeft één WAN (wan1, standaard `port1`), altijd **statisch** (IP/prefix + gateway). Een branch heeft wan1 en optioneel wan2, altijd **DHCP**. Elke branch-WAN bouwt een tunnel naar wan1 van elke hub, genoemd naar hub en branch-WAN: `H1-WAN1`, `H2-WAN1` (vanaf wan1) en `H1-WAN2`, `H2-WAN2` (vanaf wan2). De Orchestrator noemt ze `H1_WAN1`/`H1_WAN1_2`; de tool hernoemt ze overal in de gegenereerde config; de hub heeft één dial-up tunnel `EDGE_WAN1`. Elke hub heeft naast de BGP-loopback `Lo` (veld *Loopback*) de health-check-loopback `Lo-HC` 10.200.99.1/32 (vast, uit de Orchestrator); de branches meten daar naartoe via elke tunnel.
- FortiFlex-tokens voert de gebruiker in op de UI (per device + één voor de FortiManager). De tool stuurt `execute vm-license <token>` via SSH, beantwoordt de `(y/n)`-vraag, wacht op de reboot en controleert daarna `get system status` (serienummer moet overeenkomen met de invoer).
- Eerste login met leeg wachtwoord (factory): de verplichte wachtwoordwissel wordt afgehandeld met het door jou opgegeven nieuwe admin-wachtwoord.
- FortiFlex-tokens en wachtwoorden worden niet opgeslagen en nooit gelogd (`********`); alleen de Proxmox-token-secret wordt bewaard (zie *Servers*); tokens worden gevalideerd (alleen letters/cijfers) voordat ze in een CLI-regel komen.
- **Bereikbaarheid:** de container moet de FortiGates/FMG kunnen bereiken op hun *initiële IP* (factory `192.168.1.99`, of het DHCP-adres) en, bij een statisch wan1, daarna op het nieuwe adres. Het initiële IP moet uniek zijn per device. Zonder die routing kan de container niet bootstrappen; draai dan eerst de bootstrap handmatig/console en gebruik alleen de FMG-stappen.
- De Orchestrator zet wan1 in VRF 1 en reset `allowaccess` naar `ping`. Daarom zet de tool `allowaccess` op wan1 expliciet (standaard `ping https ssh fgfm`), zet voor het statische hub-wan1 de gateway op het SD-WAN-lid (de SD-WAN default route, seq 100, gebruikt die; een aparte static route op wan1 weigert FortiManager als dubbele route 1/100) en zet `central-management` `vrf-select 1` zodat de FMG-tunnel de VRF-verhuizing overleeft.

De UI heeft een **dry-run** (standaard aan): alle schrijvende calls worden gelogd maar niet verstuurd.

## Status / bekende beperkingen

- Getest: renderer (IP/VRF/BGP-regels identiek aan Fortinet's `rendered/multi_vrf`), validatie, web-UI, de volledige pipeline **tegen een mock-FMG** (`tests/e2e.py`) en de ZTP-bootstrap over **echte SSH tegen een nagebootste FortiOS-CLI** (`tests/test_bootstrap.py`, `tests/test_pipeline_ztp.py`).
- Day-0/Proxmox getest tegen een **nagebootste Proxmox-API** (`tests/fake_proxmox.py`): streaming-upload byte-voor-byte, ISO-inhoud teruggelezen, VM-parameters, NIC's, opruimen, idempotentie en rollback (`tests/test_day0_units.py`, `tests/test_pipeline_day0.py`).
- **Niet getest tegen echte Proxmox 9 / FortiGate-VM's / FortiManager-VM**: of de FortiManager de config-drive leest (niet bevestigd; zo niet: methode `ssh`), het live loskoppelen van de ISO, de token-rechten (controleer op een FortiGate met `diagnose debug cloudinit show` of de config-drive is gevonden), de exacte prompts (wachtwoordwissel, `(y/n)` bij `execute vm-license`), of FortiManager `execute vm-license <token>` kent, en `vrf-select` onder `central-management`. De nepserver is geschreven uit documentatie, niet uit een echt apparaat.
- **Getest tegen een echte FortiManager 7.6.8** (ADOM, model devices, variabelen, system/SD-WAN/static-route/CLI-templates, policy packages, template groups, blueprints aangemaakt en teruggelezen; herhaalbaar). **Nog niet getest**: FortiManager 8.0, en de stap *install* (er waren geen online FortiGates). Vroeger genoteerd als niet getest: de JSON-RPC paden/payloads in `app/fmg.py` (ADOM, `add_model` device, template-/variabele-/package-/blueprint-objecten in `app/fmgapply.py`, install); de paden zijn overgenomen uit de Postman-collectie van `sdwan-fmg-provision` moeten in een lab worden geverifieerd.
- `install.sh` is getest in een schone Debian 12 (Docker, `tests/test_install_docker.sh`) en `deploy.sh` tegen nagemaakte `pct`/`pveam`/`pvesh`/`pvesm` (`tests/test_deploy_fake_pve.sh`); het publieke `curl | bash`-pad is getest in een schone Debian 12 (`tests/test_install_remote_docker.sh`); het echte `pct create` op Proxmox 9 is nog niet gedraaid.
- WAN-poorten: hub alleen wan1 (statisch), branch wan1 + optioneel wan2 (DHCP). Alle overlays zijn van het type `WAN1`; branch-tunnels heten `H<n>-WAN<m>` (hub n, branch-WAN m), de hub-tunnel `EDGE_WAN1`. MPLS wordt niet gebruikt. Oude CSV-kolommen (`wan1_mode`, `wan1_hub_ip`, `wan2_ip`, `isp*`) worden bij import genegeerd met een melding.
- Hub-naar-hub tunnels binnen de regio staan standaard uit (zoals in de referentie); aan te zetten in de UI.
- Verificatiefase (tunnels/BGP per VRF) is nog niet gebouwd.

## Lokaal draaien / testen

```bash
git clone https://github.com/fortinet-solutions-cse/sdwan-advpn-reference.git ../sdwan-advpn-reference
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ORCH_DIR=../sdwan-advpn-reference .venv/bin/uvicorn app.main:app --port 8010
.venv/bin/python tests/mock_fmg.py 8099     # tweede terminal
.venv/bin/python tests/e2e.py               # HTTP + mock-FMG
.venv/bin/python tests/test_bootstrap.py    # ZTP over SSH tegen nep-FortiOS
ORCH_DIR=../sdwan-advpn-reference .venv/bin/python tests/test_pipeline_ztp.py
ORCH_DIR=../sdwan-advpn-reference .venv/bin/python tests/test_scope.py   # alles / alleen FMG / alleen FortiGates
.venv/bin/python tests/test_fmg_settings.py   # timers, tijdzone, ADOM-modus, FortiAnalyzer + herstart
.venv/bin/python tests/probe_fmg_settings.py <fmg-ip> [admin] [--apply]   # echte FMG: tonen, met --apply ook zetten (vraagt wachtwoord)
```
