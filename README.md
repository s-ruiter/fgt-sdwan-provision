# fgt-sdwan-provision

Deploy tool voor een Fortinet **SD-WAN/ADVPN multi-VRF** topologie via **FortiManager 8.0.x**:
2 hubs + 3 branches in één regio, ontworpen als LXC op Proxmox met een web-UI.

- Config komt uit Fortinet's [Jinja Orchestrator](https://github.com/fortinet-solutions-cse/sdwan-advpn-reference) (release 7.6, `dynamic-bgp-on-lo`, multi-VRF), **offline gerenderd** in deze tool. Er worden **geen Jinja-templates in FortiManager** gebruikt.
- De gerenderde CLI gaat per device als CLI-script (target: device database) naar FortiManager.
- ADOM-naam en serienummers geeft de gebruiker op.

## Installeren (op de Proxmox-host, als root)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/s-ruiter/fgt-sdwan-provision/main/ct/deploy.sh)"
```

Overrides via environment: `CTID`, `CT_IP` (`dhcp` of `a.b.c.d/nn`), `CT_GW`, `STORAGE`, `BRIDGE`, `VLAN`, `CORES`, `MEMORY`, `DISK_GB`.
Daarna: `http://<ct-ip>:8000`. Updaten: `pct exec <CTID> -- bash /opt/fgt-sdwan-provision/ct/install.sh`.

De Orchestrator-repo heeft geen licentiebestand en wordt daarom bij de installatie geclonet (vastgepind op een commit) in plaats van hier meegeleverd.

## Uitrolstappen (zichtbaar in de UI)

1. **FortiManager ZTP** *(optioneel)* – vast IP, route, DNS, hostnaam en FortiFlex-licentie via SSH
2. **Preflight** – login + versiecheck (8.0.x)
3. **ADOM** – aanmaken (bestaand: hergebruiken)
4. **Model devices** – 5 devices met opgegeven serienummers
5. **FortiGate ZTP** *(optioneel, parallel)* – wan1 (port1) statisch of DHCP, DNS, `central-management` naar FMG en FortiFlex-licentie
6. **Configuratie** – per device script aanmaken/bijwerken + uitvoeren op de device database
7. **Install** – wacht tot devices online zijn in FMG en installeert dan (offline devices = waarschuwing, geen fout)

## ZTP / FortiFlex

- De FortiManager heeft altijd een vast IP (`a.b.c.d/nn` + gateway). Per FortiGate kies je in de UI **statisch of DHCP** voor wan1 (= `isp1_intf`, standaard `port1`).
- FortiFlex-tokens voert de gebruiker in op de UI (per device + één voor de FortiManager). De tool stuurt `execute vm-license <token>` via SSH, beantwoordt de `(y/n)`-vraag, wacht op de reboot en controleert daarna `get system status` (serienummer moet overeenkomen met de invoer).
- Eerste login met leeg wachtwoord (factory): de verplichte wachtwoordwissel wordt afgehandeld met het door jou opgegeven nieuwe admin-wachtwoord.
- Tokens en wachtwoorden worden niet opgeslagen en nooit gelogd (`********`); tokens worden gevalideerd (alleen letters/cijfers) voordat ze in een CLI-regel komen.
- **Bereikbaarheid:** de container moet de FortiGates/FMG kunnen bereiken op hun *initiële IP* (factory `192.168.1.99`, of het DHCP-adres) en, bij een statisch wan1, daarna op het nieuwe adres. Het initiële IP moet uniek zijn per device. Zonder die routing kan de container niet bootstrappen; draai dan eerst de bootstrap handmatig/console en gebruik alleen de FMG-stappen.
- De Orchestrator zet wan1 in VRF 1 en reset `allowaccess` naar `ping`. Daarom zet de tool `allowaccess` op wan1 expliciet (standaard `ping https ssh fgfm`), voegt voor statisch wan1 de default route toe en zet `central-management` `vrf-select 1` zodat de FMG-tunnel de VRF-verhuizing overleeft.

De UI heeft een **dry-run** (standaard aan): alle schrijvende calls worden gelogd maar niet verstuurd.

## Status / bekende beperkingen

- Getest: renderer (IP/VRF/BGP-regels identiek aan Fortinet's `rendered/multi_vrf`), validatie, web-UI, de volledige pipeline **tegen een mock-FMG** (`tests/e2e.py`) en de ZTP-bootstrap over **echte SSH tegen een nagebootste FortiOS-CLI** (`tests/test_bootstrap.py`, `tests/test_pipeline_ztp.py`).
- **Niet getest tegen echte FortiGate-VM's/FortiManager-VM**: de exacte prompts (wachtwoordwissel, `(y/n)` bij `execute vm-license`), of FortiManager `execute vm-license <token>` kent, en `vrf-select` onder `central-management`. De nepserver is geschreven uit documentatie, niet uit een echt apparaat.
- **Niet getest tegen een echte FortiManager 8.0**: de JSON-RPC paden/payloads in `app/fmg.py` (ADOM aanmaken, `add_model` device, script exec, install) moeten in een lab worden geverifieerd.
- `deploy.sh` is niet gedraaid op een echte Proxmox-host.
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
