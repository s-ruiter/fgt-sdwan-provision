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

1. **Preflight** – login + versiecheck (8.0.x)
2. **ADOM** – aanmaken (bestaand: hergebruiken)
3. **Model devices** – 5 devices met opgegeven serienummers
4. **Configuratie** – per device script aanmaken + uitvoeren op de device database
5. **Install** – device settings installeren (devices die nog offline zijn geven een waarschuwing, geen fout)

De UI heeft een **dry-run** (standaard aan): alle schrijvende calls worden gelogd maar niet verstuurd.

## Status / bekende beperkingen

- Getest: renderer (IP/VRF/BGP-regels identiek aan Fortinet's `rendered/multi_vrf`), validatie, web-UI en de volledige pipeline **tegen een mock-FMG** (`tests/e2e.py`).
- **Niet getest tegen een echte FortiManager 8.0**: de JSON-RPC paden/payloads in `app/fmg.py` (ADOM aanmaken, `add_model` device, script exec, install) moeten in een lab worden geverifieerd.
- `deploy.sh` is niet gedraaid op een echte Proxmox-host.
- De FortiGates moeten zelf nog naar de FortiManager wijzen (`config system central-management`) en geautoriseerd worden; WAN-interfaces gebruiken zoals in de referentie DHCP.
- Hub-naar-hub tunnels binnen de regio staan standaard uit (zoals in de referentie); aan te zetten in de UI.
- Verificatiefase (tunnels/BGP per VRF) is nog niet gebouwd.

## Lokaal draaien / testen

```bash
git clone https://github.com/fortinet-solutions-cse/sdwan-advpn-reference.git ../sdwan-advpn-reference
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ORCH_DIR=../sdwan-advpn-reference .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/python tests/mock_fmg.py 8099 &   # in een tweede terminal; en tests/e2e.py (poorten in het script)
```
