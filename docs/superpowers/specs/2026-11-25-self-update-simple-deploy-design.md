# Self-update and a simple Deploy page

## Why
The tool should be usable by people who did not build the lab: updating it without SSH, and a Deploy page that only
asks what they must know about their own environment.

## 1. About goes, self-update comes
- The About page and its nav button are removed (`/api/about` stays: the Progress page uses its step list).
- The tool knows its own commit (`git rev-parse HEAD` in /opt/fgt-sdwan-provision) and checks GitHub `main` of the
  public repo (`https://api.github.com/repos/s-ruiter/fgt-sdwan-provision/compare/<own>...main`, cached ~10 min).
- When `main` is newer, the nav shows **"Update available"**. A click opens a small panel: current and new version and the
  titles of the new commits, and an **Update** button.
- Update = `systemd-run --unit fgt-sdwan-update` running `ct/install.sh` (the same script as now, keeps the port), so it
  survives the restart of the service. The page shows "updating… the tool restarts" and reloads when the new version
  answers.
- Refused while a deployment or cleanup runs (same rule as for a manual update). No GitHub reachable -> no message (a
  quiet "could not check for updates" in the panel).

## 2. Simple Deploy page (default) and Advanced
- A switch **Simple / Advanced** at the top of the Deploy page (remembered per browser). Advanced = the page as it is now.
- **Simple shows** what talks to the outside world:
  - the deployment choice (Everything selected, as now);
  - Proxmox: server, node, storages, default bridge, the WAN-emulator bridge and the uplink bridge, images;
  - uplink to the lab: VLAN, IP, gateway;
  - FortiManager: IP/prefix, gateway (admin user stays `admin`);
  - FortiFlex: licences as now (check licences);
  - **one password** for FortiManager, FortiGates, LAN clients and Traffic-Gen (strong: 8+ characters, upper, lower, digit,
    special character; show/hide);
  - **Multi-VRF** checkbox; when on, DIA is on for both WAN links (as the defaults already are);
  - the topology diagram;
  - saved settings (load/save/export/import).
- **Simple hides** (defaults stay in force): hub/spoke cards (always 2 hubs + 3 spokes with the default names, loopbacks,
  ports, LANs), VRF table, ISP blocks/VLAN bases, LAN VLAN bases, port table, VM sizes, WAN-emulator container details,
  port forward bases, separate passwords.
- **Traffic-Gen is always deployed** (the checkbox goes). Its management IP is derived: the uplink IP minus one
  (e.g. wan-emu 10.10.106.250 -> Traffic-Gen 10.10.106.249); in Advanced it can be changed.
- Switching to Simple on a form that differs from the simple defaults (e.g. 4 spokes from a saved setting) keeps those
  values and says so ("this setting has a custom topology: shown in Advanced").
- In Advanced the four passwords can be set separately; the generic password fills all four until one is changed.

## Calls I made (check them)
- Deployment choice: the list stays visible in Simple, with Everything selected (your "the default selection is good").
- Traffic-Gen IP = uplink IP − 1 when empty.
- The Simple/Advanced choice is remembered per browser (localStorage).

## Tests
- API: version check (fake GitHub: newer / same / unreachable), update refused during a job, update started via
  systemd-run (command captured, not run).
- UI static: no About nav/section, update badge/panel ids, Simple/Advanced switch, generic password field, no `tg_on`.
- Model: Traffic-Gen always on with Everything/FortiGates, IP derived from the uplink.
