# Licences: FortiFlex or licence files

## Why
Not everyone has a FortiFlex account. With ordinary FortiCare licence files (`.lic`) the tool must deploy too.

## Decisions (Sander)
- The tab **FortiFlex** becomes **Licenses**, with a choice at the top: **FortiFlex** or **License files**.
- License files: the FortiFlex fields disappear; an upload per device (FortiManager + every FortiGate).
- Files are named after the serial number (FortiCare: `FGVM08TM24001234.lic`, `FMG-VMTM24005678.lic`): the tool reads
  the serial from the file name.
- Switching to licence files keeps the FortiFlex configuration (API user, configurations) untouched; switching back
  uses it again.
- Uploaded licence files stay after a deployment and are reused by later deployments (until replaced or deleted).

## Design
- **Mode**: one tool setting `licensing` = `fortiflex` | `files` (database). Default `fortiflex` (today's behaviour).
  The FortiFlex settings stay in their own rows; the mode only decides which one the deployment uses.
- **Storage**: `<DATA_DIR>/licenses/<device>.lic` (dir 0700, files 0600) + metadata `{device: {serial, file, size,
  uploaded}}`. The device key is the device name (`fmg` = the FortiManager, `site1-H1`, …).
- **Licenses page (files mode)**: a table with one row per device of the current deployment form (FortiManager first,
  then hubs and spokes; in Simple the standard 2 hubs + 3 spokes): device, serial (from the file name), file, uploaded,
  **Upload** / **Replace** / **Delete**. A file whose name is not a FortiGate-VM / FortiManager-VM serial
  (`FGVM…`, `FMG-VM…`) is refused for the wrong kind of device, with a message. The same serial on two devices is refused.
- **Deployment (files mode)**:
  - no FortiFlex calls; the serial numbers come from the files (into the model devices, as FortiFlex does today);
  - the Day-0 config drive carries the licence file (OpenStack config drive: `openstack/content/0000` + `files` entry in
    `meta_data.json`), for FortiGates and the FortiManager, instead of the FortiFlex token;
  - a device without a licence file -> validation error before anything is created ("site1-2: no licence file (Licenses page)");
  - files are only read, never removed.
- **Scope**: licence files are for VMs created by the tool (Day-0). The SSH/ZTP path for existing VMs keeps FortiFlex
  tokens only (unchanged).
- **Deploy page**: the "Check licences" button and the FortiFlex token fields follow the mode (in files mode the
  check shows which devices have a file).

## Not verified yet
- That FortiGate-VM 8.0.2 and FortiManager-VM 8.0.2 on KVM take the licence from the config drive this way. To be
  checked first with one real licence file in the lab (spike), before the rest is built on it.

## Tests
- Mode setting kept; FortiFlex settings untouched when switching.
- Upload: serial from file name, wrong kind / duplicate serial refused, file 0600, kept after a (fake) deployment.
- Deployment in files mode: no FortiFlex calls, serials in the model devices, config drive contains the licence file,
  missing file -> error before creation.
- UI static: tab renamed, mode choice, upload table.
