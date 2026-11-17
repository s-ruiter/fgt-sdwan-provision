# fgt-sdwan-provision

_UI: dezelfde look & feel als [Spawnpad](https://github.com/s-ruiter/Spawnpad) (Geist, rode accenten, kaarten; lettertype onder SIL OFL, zie `static/fonts/LICENSE.TXT`)._

Deploy tool voor een Fortinet **SD-WAN/ADVPN multi-VRF** topologie via **FortiManager 8.0.x**:
2 hubs + 3 branches in één regio, ontworpen als LXC op Proxmox met een web-UI.

- Config komt uit Fortinet's [Jinja Orchestrator](https://github.com/fortinet-solutions-cse/sdwan-advpn-reference) (release 7.6, `dynamic-bgp-on-lo`, multi-VRF), **offline gerenderd** in deze tool. Er worden **geen Jinja-templates in FortiManager** gebruikt.
- De Orchestrator wordt *symbolisch* gerenderd: per-device waarden worden FortiManager-metadata-variabelen (`$(loopback)`, `$(lan_ip_netmask)` ...). Daaruit maakt de tool **native FortiManager-objecten**: CLI-templates (underlay/overlay/routing/inter-VRF) + CLI-template-group, system template (DNS), native SD-WAN-template, static-route-template, policy package, interfaces/adressen/IP-pools, device group, template group en device blueprint. Eén set per hub (HUB1, HUB2) en per groep branches met identieke config (SPOKES-n; zelfde poortnamen vereist voor de native SD-WAN-template).
- ADOM-naam en serienummers geeft de gebruiker op.
- **CSV import/export** (kaart *Topologie*): *CSV downloaden* geeft alleen de **devices** (`[devices]`, LAN-kolommen volgens de gekozen modus). *Alles exporteren naar CSV* (onderaan de branches) geeft ook `[algemeen]` (ADOM, DNS, PSK, regio, FortiManager), `[vrf]` en `[proxmox]` (servernaam, node, storages, VM-groottes, poort-bridges) zodat een uitrol herhaald kan worden. Bij importeren neemt de tool standaard alleen de devices over; bevat het bestand ook instellingen, dan vraagt hij of die ook overgenomen moeten worden. Tokens en wachtwoorden staan nooit in het bestand; de IPsec-PSK wel (leesbaar).
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

Per product (FortiManager / FortiGates) kies je een **methode**: `day0` (de tool maakt de VM op Proxmox, cloud-init), `ssh` (bestaande VM, bootstrap over SSH) of `none`.

1. **FortiManager-VM** *(day0)* – image naar Proxmox, VM + datadisk + Day-0 config-drive ISO, starten
2. **FortiManager ZTP** – day0: wachten op het vaste IP (licentie + reboot) · ssh: vast IP, route, DNS en `execute vm-license`
3. **Preflight** – login + versiecheck (8.0.x); daarna wordt de FMG-ISO losgekoppeld en verwijderd
4. **ADOM** – aanmaken (bestaand: hergebruiken)
5. **Model devices** – 5 devices met opgegeven serienummers
6. **FortiGate-VM's** *(day0)* – per FortiGate een VM + ISO (hostname, admin-wachtwoord, wan1 statisch/DHCP, DNS, `central-management`, FortiFlex-token), starten
7. **FortiGate ZTP** – day0: wachten tot ze gelicenseerd online zijn in FMG, daarna ISO's opruimen · ssh: zie hieronder
8. **Templates** – variabelen, system/SD-WAN/CLI-templates, policy packages, template groups, blueprints (alleen set/replace/update: herhaalbaar)
9. **Install** – policy package per rol op de online devices (offline = waarschuwing, geen fout)

## Images uploaden + Day-0 (cloud-init)

- Download bij FortiCloud (*Support > Firmware Downloads*) het `FGT_VM64_KVM-v…out.kvm.zip` en `FMG_VM64_KVM-v…out.kvm.zip` en upload ze in de UI (sectie *Proxmox & images*). De container pakt de qcow2 uit, controleert hem, toont versie en **sha256** (vergelijk met de portal) en bewaart hem in `/var/lib/fgt-sdwan/images` (LXC-disk standaard 12 GB).
- De tool praat uitsluitend met de **Proxmox-API (token, PVE 9+)**: upload naar een storage met content-type *Import*, VM aanmaken met `import-from`, ISO uploaden naar een ISO-storage. Geen SSH naar Proxmox.
- **Proxmox-gegevens als dropdowns:** na het kiezen van een server haalt de tool node, disk-/import-/ISO-storages (alleen actieve, per node) en bridges rechtstreeks uit Proxmox op; ook de bridge per poort en de FMG-bridge zijn dropdowns. Eerder gekozen waarden blijven staan.
- **Wachtwoorden:** per product één veld (FortiManager, FortiGates) dat voor admin én SSH geldt. Bij een factory-apparaat wordt dit wachtwoord ingesteld; heeft het apparaat het al, dan wordt het gebruikt om in te loggen.
- **Servers (menu *Servers*):** Proxmox-servers voeg je één keer toe (naam, URL, token-ID, token-secret) en kies je daarna bij elke uitrol uit een lijst. De **token-secret wordt bewaard** in `/var/lib/fgt-sdwan/fgt-sdwan.db` (SQLite, rechten 0600, map 0700) en nooit door de API teruggegeven; bij bewerken laat je hem leeg om hem te behouden (wijzig je URL of token-ID, dan moet hij opnieuw). Per server onthoudt de tool ook je laatste keuzes (node, storages, bridge per poort, VM-grootte, licentiemodus) en vult die de volgende keer vanzelf in; de laatst gebruikte server wordt voorgeselecteerd. De database blijft bij updates (`install.sh`) en herstarts staan; maak er een back-up van als je hem wilt bewaren.
- **Let op:** de tool heeft geen login. Wie het web-adres kan bereiken kan met de opgeslagen token VM's aanmaken op die Proxmox. Draai hem alleen op een vertrouwd netwerk en geef de token alleen de rechten die nodig zijn.
- Per VM bouwt de tool een config-drive ISO (label `config-2`, `openstack/latest/user_data`). De licentie wordt toegepast met `execute vm-license <token>` als **laatste CLI-regel** (FortiFlex, `cli`; dezelfde methode als Spawnpad). De UI biedt geen andere licentiemodus aan; de MIME-variant (`config.txt` + `license.txt`) zit alleen nog in de backend en wordt niet gebruikt.
- NIC's: één virtio-NIC per poort tot de hoogste `portN` die het device gebruikt; bridge/VLAN per poort in de UI. FMG: 1 NIC + datadisk.
- **ISO opruimen:** de ISO bevat admin-wachtwoord en (eenmalig) token. Zodra de VM gelicenseerd/online is, wordt de ISO geeject (`ide2: none`) en van de storage verwijderd.
- Idempotent op VM-naam (tag `fgt-sdwan`): een tweede run hergebruikt bestaande VM's. Faalt het aanmaken, dan worden de VM's, imports en ISO's van die stap weer verwijderd.
- Proxmox-token: minimaal o.a. `VM.Allocate`, `VM.Config.*`, `VM.PowerMgmt`, `VM.Audit`, `Datastore.AllocateSpace`, `Datastore.AllocateTemplate`, `Datastore.Audit`, `Sys.Audit`, `SDN.Use` op de gebruikte bridges (nog niet tegen echte PVE getest; bij 403 toont de uitrol de Proxmox-melding).

## ZTP / FortiFlex (methode `ssh`, voor bestaande VM's)

- De FortiManager heeft altijd een vast IP (`a.b.c.d/nn` + gateway). Per FortiGate kies je in de UI **statisch of DHCP** voor wan1 (= `isp1_intf`, standaard `port1`).
- FortiFlex-tokens voert de gebruiker in op de UI (per device + één voor de FortiManager). De tool stuurt `execute vm-license <token>` via SSH, beantwoordt de `(y/n)`-vraag, wacht op de reboot en controleert daarna `get system status` (serienummer moet overeenkomen met de invoer).
- Eerste login met leeg wachtwoord (factory): de verplichte wachtwoordwissel wordt afgehandeld met het door jou opgegeven nieuwe admin-wachtwoord.
- FortiFlex-tokens en wachtwoorden worden niet opgeslagen en nooit gelogd (`********`); alleen de Proxmox-token-secret wordt bewaard (zie *Servers*); tokens worden gevalideerd (alleen letters/cijfers) voordat ze in een CLI-regel komen.
- **Bereikbaarheid:** de container moet de FortiGates/FMG kunnen bereiken op hun *initiële IP* (factory `192.168.1.99`, of het DHCP-adres) en, bij een statisch wan1, daarna op het nieuwe adres. Het initiële IP moet uniek zijn per device. Zonder die routing kan de container niet bootstrappen; draai dan eerst de bootstrap handmatig/console en gebruik alleen de FMG-stappen.
- De Orchestrator zet wan1 in VRF 1 en reset `allowaccess` naar `ping`. Daarom zet de tool `allowaccess` op wan1 expliciet (standaard `ping https ssh fgfm`), voegt voor statisch wan1 de default route toe en zet `central-management` `vrf-select 1` zodat de FMG-tunnel de VRF-verhuizing overleeft.

De UI heeft een **dry-run** (standaard aan): alle schrijvende calls worden gelogd maar niet verstuurd.

## Status / bekende beperkingen

- Getest: renderer (IP/VRF/BGP-regels identiek aan Fortinet's `rendered/multi_vrf`), validatie, web-UI, de volledige pipeline **tegen een mock-FMG** (`tests/e2e.py`) en de ZTP-bootstrap over **echte SSH tegen een nagebootste FortiOS-CLI** (`tests/test_bootstrap.py`, `tests/test_pipeline_ztp.py`).
- Day-0/Proxmox getest tegen een **nagebootste Proxmox-API** (`tests/fake_proxmox.py`): streaming-upload byte-voor-byte, ISO-inhoud teruggelezen, VM-parameters, NIC's, opruimen, idempotentie en rollback (`tests/test_day0_units.py`, `tests/test_pipeline_day0.py`).
- **Niet getest tegen echte Proxmox 9 / FortiGate-VM's / FortiManager-VM**: of de FortiManager de config-drive leest (niet bevestigd; zo niet: methode `ssh`), het live loskoppelen van de ISO, de token-rechten (controleer op een FortiGate met `diagnose debug cloudinit show` of de config-drive is gevonden), de exacte prompts (wachtwoordwissel, `(y/n)` bij `execute vm-license`), of FortiManager `execute vm-license <token>` kent, en `vrf-select` onder `central-management`. De nepserver is geschreven uit documentatie, niet uit een echt apparaat.
- **Getest tegen een echte FortiManager 7.6.8** (ADOM, model devices, variabelen, system/SD-WAN/static-route/CLI-templates, policy packages, template groups, blueprints aangemaakt en teruggelezen; herhaalbaar). **Nog niet getest**: FortiManager 8.0, en de stap *install* (er waren geen online FortiGates). Vroeger genoteerd als niet getest: de JSON-RPC paden/payloads in `app/fmg.py` (ADOM, `add_model` device, template-/variabele-/package-/blueprint-objecten in `app/fmgapply.py`, install); de paden zijn overgenomen uit de Postman-collectie van `sdwan-fmg-provision` moeten in een lab worden geverifieerd.
- `install.sh` is getest in een schone Debian 12 (Docker, `tests/test_install_docker.sh`) en `deploy.sh` tegen nagemaakte `pct`/`pveam`/`pvesh`/`pvesm` (`tests/test_deploy_fake_pve.sh`); het publieke `curl | bash`-pad is getest in een schone Debian 12 (`tests/test_install_remote_docker.sh`); het echte `pct create` op Proxmox 9 is nog niet gedraaid.
- Alleen wan1 is statisch/DHCP te kiezen; ISP2/MPLS blijven DHCP zoals in de referentie.
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
```
