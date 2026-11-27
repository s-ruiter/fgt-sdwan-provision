# Lab start / stop — design

## Intent and why

Sander runs demo labs that this tool deploys on Proxmox. Between demos he wants to switch a lab off cleanly (VMs and
containers shut down, FortiFlex licences stopped so they cost no points) and later switch the same lab on again
(licences reactivated first, then everything started), without redeploying. Deleting a lab stays possible.

## Goals

- The tab **Cleanup** is renamed **Lab** (route `#/lab`; `#/cleanup` keeps working as an alias). It shows, for the chosen
  Proxmox server and node, the lab that is there (if any) with three actions: **Start lab**, **Stop lab**,
  **Delete lab** (the existing cleanup, unchanged in what it deletes).
- "The lab" = exactly what Cleanup already finds: every VM and container with the tool's tag on that node, except the
  deploy container itself (same rules as `app/cleanup.py:plan`). Files (ISOs, imports) play no part in start/stop.
- The overview shows each VM/CT with its state (running / stopped) and, when FortiFlex is configured, the licences
  that belong to it with their status — before anything is done.
- **Stop lab** runs as a job (Progress page, log, steps, Cancel):
  1. shut down in order: LAN clients and Traffic-Gen → FortiGates → wan-emu → FortiManager(s);
     graceful shutdown (Proxmox `shutdown`), and when a guest is not off after a timeout, a hard stop;
     guests already stopped are skipped;
  2. then, when FortiFlex is configured and the user ticked it (default on), stop the FortiFlex licences of exactly
     these devices.
- **Start lab** runs as a job:
  1. when FortiFlex is configured: reactivate the stopped licences of these devices (same serial; no new licence);
  2. start in order: FortiManager(s) → wan-emu → FortiGates → LAN clients and Traffic-Gen; guests already running
     are skipped;
  3. wait until the FortiManager answers, then check per FortiGate that it is online in FortiManager again (with a
     timeout; a FortiGate that does not come back is a warning in the log, not a failed job).
- Delete lab: the existing cleanup flow (overview, "Also stop the FortiFlex licences", type DELETE).
- Start/Stop are refused while a deployment, cleanup or another lab job is running (as deploy and cleanup already
  refuse each other), and a deployment is refused while a lab job runs.

## Non-goals

- No start/stop buttons on the Dashboard (Sander's choice).
- No per-device start/stop, no scheduling, no several labs per node told apart (one node = one lab, as Cleanup today).
- No new FortiFlex licences or tokens on start; no redeploy.

## Anti-goals

- Stopping or reactivating a licence of a device that is not in this lab; issuing a new licence on Start.
- A FortiGate shut off hard while a graceful shutdown would have worked.
- Touching the deploy container.

## Constraints

- Proxmox via the existing client (`app/pve.py`); add `shutdown_vm(node, vmid, timeout)` and
  `shutdown_ct(node, vmid, timeout)` (Proxmox `POST …/status/shutdown`), keep `start_*`, `stop_*`, `*_status`.
- Roles by Proxmox name: FortiManager = the VM whose notes start with `fgt-sdwan FortiManager` or whose name (without
  the name prefix) is a known FortiManager; wan-emu = the CT named `<prefix>wan-emu`; LAN clients `<prefix>client-*`;
  Traffic-Gen the CT the tool created for it; FortiGates = the remaining tool VMs.
- **Licence ↔ VM mapping is remembered on the VM itself** (Sander's request): at deployment the tool already knows each
  device's FortiFlex serial before it creates the VM, and writes it into the Proxmox VM notes as
  `fgt-sdwan licence=<serial>` (FortiManager: `fgt-sdwan FortiManager ip=<ip> licence=<serial>`). A reused VM gets
  its notes rewritten on the next deployment. Start / Stop take the serial from the notes: the same licence goes back
  to the same FortiGate / FortiManager, never a new licence, never a name search.
- Labs deployed before this change (no `licence=` in the notes): fallback to the FortiFlex description
  `fgt-sdwan <ADOM> <device>` matched on the device name; the overview marks these as "matched by name", with a tick
  box each, and when two licences of different ADOMs match the same device the user unticks the wrong one. The job
  gets only the chosen serials.
- Jobs reuse the deploy job machinery (store, stream, Cancel), with their own step list per job kind
  (`stop`: shutdown, licences; `start`: licences, start, check) so the Progress page draws the right steps.
- Shutdown timeout per guest: 180 s (FortiManager 300 s), then hard stop.

## Settled after the builder check (Claude's calls)

- **Name prefix**: read from the lab itself: the wan-emu CT is named `<prefix>wan-emu`, so prefix = its name minus
  `wan-emu` (no wan-emu: prefix ""). Device name = Proxmox name without that prefix. The existing `_licences_for`
  compares the description's device to the full Proxmox name; it is fixed to use the prefix-less name (with a prefix
  it matched nothing).
- **FortiManager role**: a tool VM whose notes start with `fgt-sdwan FortiManager`, or whose Proxmox name is the `vm`
  of a FortiManager the tool remembers (Remote-access logins, `access_creds.fmgs[*].vm`), or whose device name starts
  with `fmg`.
- **Start step 3 (FortiGates back in FortiManager)**: uses the remembered login of the lab's FortiManager (matched by
  its `vm` name) and checks the devices over all ADOMs (`/dvmdb/device`, connection status per name), so no ADOM is
  needed. No remembered login → the check is skipped with a note in the log. Several FortiManagers in the lab → the
  check runs against each that has a login.
- **Licences — statuses**: Stop acts on ACTIVE/PENDING, Start on STOPPED; other statuses (e.g. EXPIRED) are shown in
  the overview and skipped. FortiManager licences take part like FortiGate ones. Licence-file mode (no FortiFlex): no
  licence steps, the overview says so. The overview lists the matched licences with a tick box each (default ticked);
  Start and Stop get the chosen serials.
- **Licences — reactivation**: Start calls FortiFlex *reactivate* only, no new token: an existing VM keeps its licence
  and serial and re-validates with FortiCare after reactivation (a token is only needed for a first activation; a new
  token would invalidate nothing the VM still needs, but is not used). **To verify on the first real run**: the Start
  job reports per FortiGate whether it comes back online in FortiManager; if a VM turns out to need a token after a
  reactivation, that becomes a follow-up (apply a new token over SSH).
- **Cancel / failures**: Cancel stops after the current guest (the waits are cancellable); on Stop the licence step is
  then skipped. A guest that fails to shut down or start: logged, the job goes on with the rest and ends with
  warnings. A licence that fails to reactivate: warning, its VM is still started.
- **Concurrency**: one global lock, as deploy and cleanup today: a lab job refuses while a deployment, cleanup or
  another lab job runs (any server/node), and deploy / cleanup refuse while a lab job runs. A restart marks a running
  lab job interrupted (as deploy jobs); the Lab page always shows the live state per guest.
- **Progress page**: lab jobs are ordinary jobs (newest becomes "latest"), with `steps_def` in the job; jobs without
  it (older deployments) use the deploy steps. `dry_run` false.
- Smaller: `onboot` is left alone; Stop waits per guest until it is off (or the timeout) before the next one;
  stored state (`wanemu_addr`, forwards, logins) is not touched.

## Approaches

1. **Start/Stop as jobs on the Progress page** — chosen. Same progress view, log and Cancel as a deployment; fits a
   process of minutes with licences; a closed page loses nothing.
2. A button that waits for the result (like Cleanup today) — simpler, but little feedback and the result is lost when
   the page is closed.

## Left to the builder

- Exact layout of the Lab page (overview table with state and licences, the three actions in their own cards).
- How the Progress page receives the job's step list (e.g. `steps_def` in the job).
- Wording of log lines and hints.
- Tests in the project's style (fake Proxmox / fake FortiFlex exist in `tests/`).
