"""Lab start / stop: the VMs and containers this tool deployed on one Proxmox node (the same set Cleanup deletes),
switched off and on in a safe order, with their FortiFlex licences stopped and reactivated.

Each VM's licence comes from its Proxmox notes (`licence=<serial>`, written at deployment), so a device always gets
its own licence back. VMs deployed before that have no serial in their notes: their licences are matched by the
device name in the FortiFlex description and marked as such; the user confirms them.
"""
from .cleanup import PROTECT
from .model import TAG_VM
from .provision_vms import note_serial

STOP_RANK = {"client": 0, "tgen": 0, "fgt": 1, "wanemu": 2, "fmg": 3}      # Start: the reverse
WANEMU = "wan-emu"


def prefix_of(cts):
    """The deployment's name prefix, read from its wan-emu container (<prefix>wan-emu); "" without one."""
    for c in cts:
        n = str(c.get("name") or "")
        if n.endswith(WANEMU):
            return n[: -len(WANEMU)]
    return ""


def _role(kind, device, note, name, fmg_vms):
    if kind == "ct":
        if device == WANEMU:
            return "wanemu"
        if device == "trafficgen":
            return "tgen"
        return "client"
    if str(note).startswith("fgt-sdwan FortiManager") or name in fmg_vms or device.startswith("fmg"):
        return "fmg"
    return "fgt"


def inventory(pve, node, fmg_vms=frozenset()):
    """[{kind, vmid, name, device, role, status, serial}] of the lab on `node`, in Stop order (then by VMID)."""
    vms = [r for r in pve.find_vms(TAG_VM) if r.get("node", node) == node and r.get("name") not in PROTECT]
    cts = [r for r in pve.find_cts(TAG_VM) if r.get("node", node) == node and r.get("name") not in PROTECT]
    prefix = prefix_of(cts)
    out = []
    for kind, rows in (("vm", vms), ("ct", cts)):
        for r in rows:
            name = r.get("name") or str(r["vmid"])
            device = name[len(prefix):] if prefix and name.startswith(prefix) else name
            note = ""
            if kind == "vm":
                note = str((pve.vm_config(node, int(r["vmid"])) or {}).get("description") or "")
            out.append({"kind": kind, "vmid": int(r["vmid"]), "name": name, "device": device,
                        "role": _role(kind, device, note, name, fmg_vms), "status": r.get("status", ""),
                        "serial": note_serial(note) if kind == "vm" else ""})
    return sorted(out, key=lambda g: (STOP_RANK[g["role"]], g["vmid"]))


def match_licences(guests, ents):
    """[{serial, device, vm, status, description, source}]: the serial in a VM's notes ("notes"); for a VM without one
    every tool licence whose description ends in its device name ("name": the user confirms, there can be several)."""
    by_sn = {e.get("serialNumber"): e for e in ents}
    out = []
    for g in guests:
        if g["kind"] != "vm":
            continue
        if g["serial"]:
            e = by_sn.get(g["serial"], {})
            out.append({"serial": g["serial"], "device": g["device"], "vm": g["name"], "status": e.get("status", ""),
                        "description": e.get("description", ""), "source": "notes"})
            continue
        for e in ents:
            if str(e.get("description", "")).split(" ")[-1] == g["device"]:
                out.append({"serial": e.get("serialNumber"), "device": g["device"], "vm": g["name"],
                            "status": e.get("status", ""), "description": e.get("description", ""), "source": "name"})
    return out


# ---- the two jobs -------------------------------------------------------------------------------------------------
TIMEOUTS = {"default": 180, "fmg": 300, "check": 900, "poll": 15}
LABEL = {"fmg": "FortiManager", "fgt": "FortiGate", "wanemu": "WAN emulator", "client": "LAN client", "tgen": "Traffic-Gen"}


def _status(pve, node, g):
    return (pve.vm_status if g["kind"] == "vm" else pve.ct_status)(node, g["vmid"])


def stop(pve, node, guests, licences, flex, emit, timeouts=None):
    """Shut the guests down in Stop order (graceful, hard after the timeout; already off = skipped), then stop the given
    licences that are ACTIVE/PENDING. licences = [{serial, status}] the user chose; flex None = no FortiFlex. Waits are
    cancellable (cancel.Cancelled: the licences are then left alone). Returns {"warnings": n}."""
    from . import cancel
    from .pve import PveError
    t, warn = dict(TIMEOUTS, **(timeouts or {})), 0
    emit("shutdown", "running", f"shutting down {len(guests)} VM(s) / container(s): LAN clients first, FortiManager last")
    for g in sorted(guests, key=lambda g: (STOP_RANK[g["role"]], g["vmid"])):
        cancel.check()
        label = f"{g['name']} ({LABEL[g['role']]}, {g['kind'].upper()} {g['vmid']})"
        try:
            if _status(pve, node, g) != "running":
                emit("shutdown", "running", f"{label}: already off")
                continue
            to = t["fmg"] if g["role"] == "fmg" else t["default"]
            emit("shutdown", "running", f"{label}: shutting down (at most {to} s)")
            try:
                sd = pve.shutdown_vm if g["kind"] == "vm" else pve.shutdown_ct
                pve.wait_task(node, sd(node, g["vmid"], to), timeout=to + 60)
            except PveError as e:
                if _status(pve, node, g) == "running":
                    emit("shutdown", "warn", f"{label}: no clean shutdown ({e}); switching it off")
                    warn += 1
                    hs = pve.stop_vm if g["kind"] == "vm" else pve.stop_ct
                    pve.wait_task(node, hs(node, g["vmid"]))
            emit("shutdown", "running", f"{label}: off")
        except PveError as e:
            warn += 1
            emit("shutdown", "warn", f"{label}: {e}")
    emit("shutdown", "warn" if warn else "ok", "all guests off" + (f" ({warn} warning(s))" if warn else ""))
    if flex is None:
        emit("licences", "skipped", "FortiFlex not used: no licences to stop")
        return {"warnings": warn}
    todo = [l for l in licences if l.get("status") in ("ACTIVE", "PENDING")]
    for l in licences:
        if l not in todo:
            emit("licences", "running", f"{l['serial']}: {l.get('status') or 'unknown'}, left as it is")
    lw = 0
    for l in todo:
        cancel.check()
        try:
            flex.stop(l["serial"])
            emit("licences", "running", f"{l['serial']}: stopped (no points while the lab is off)")
        except Exception as e:                                   # noqa: BLE001 - one licence must not stop the rest
            lw += 1
            emit("licences", "warn", f"{l['serial']}: not stopped ({e})")
    emit("licences", "warn" if lw else "ok", f"{len(todo) - lw} licence(s) stopped")
    return {"warnings": warn + lw}


def start(pve, node, guests, licences, flex, fmg_checks, emit, timeouts=None):
    """Reactivate the given STOPPED licences (never a new licence or token), start the guests FortiManager first, LAN
    clients last (already on = skipped), then wait until the FortiGates are online in FortiManager again. fmg_checks =
    [callable() -> {device: "up"|...}] (empty: check skipped). Returns {"warnings": n}."""
    from . import cancel
    from .pve import PveError
    t, warn = dict(TIMEOUTS, **(timeouts or {})), 0
    if flex is None:
        emit("licences", "skipped", "FortiFlex not used: no licences to reactivate")
    else:
        todo = [l for l in licences if l.get("status") == "STOPPED"]
        lw = 0
        for l in todo:
            cancel.check()
            try:
                flex.reactivate(l["serial"])
                emit("licences", "running", f"{l['serial']}: reactivated (same licence, same serial)")
            except Exception as e:                               # noqa: BLE001
                lw += 1
                emit("licences", "warn", f"{l['serial']}: not reactivated ({e}); its VM is started anyway")
        warn += lw
        emit("licences", "warn" if lw else "ok", f"{len(todo) - lw} licence(s) reactivated")
    emit("start", "running", "starting: FortiManager first, LAN clients last")
    sw = 0
    for g in sorted(guests, key=lambda g: (-STOP_RANK[g["role"]], g["vmid"])):
        cancel.check()
        label = f"{g['name']} ({LABEL[g['role']]}, {g['kind'].upper()} {g['vmid']})"
        try:
            if _status(pve, node, g) == "running":
                emit("start", "running", f"{label}: already on")
                continue
            st = pve.start_vm if g["kind"] == "vm" else pve.start_ct
            pve.wait_task(node, st(node, g["vmid"]))
            emit("start", "running", f"{label}: started")
        except PveError as e:
            sw += 1
            emit("start", "warn", f"{label}: not started ({e})")
    warn += sw
    emit("start", "warn" if sw else "ok", "all guests started" + (f" ({sw} warning(s))" if sw else ""))
    fgts = [g["device"] for g in guests if g["role"] == "fgt"]
    if not fgts:
        emit("check", "skipped", "no FortiGates in this lab")
        return {"warnings": warn}
    if not fmg_checks:
        emit("check", "skipped", "no FortiManager login known to this tool: not checked whether the FortiGates are back")
        return {"warnings": warn}
    import time
    emit("check", "running", f"waiting until {len(fgts)} FortiGate(s) are online in FortiManager again")
    end, up = time.monotonic() + t["check"], set()
    while True:
        for chk in fmg_checks:
            try:
                up |= {d for d, s in (chk() or {}).items() if s in ("up", "UP", 1)}
            except Exception:                                    # noqa: BLE001 - FortiManager still booting
                pass
        if all(d in up for d in fgts) or time.monotonic() >= end:
            break
        cancel.sleep(t["poll"])
    missing = [d for d in fgts if d not in up]
    if missing:
        warn += 1
        emit("check", "warn", f"not online in FortiManager: {', '.join(missing)} (licence or boot: check its console)")
    else:
        emit("check", "ok", "all FortiGates online in FortiManager")
    return {"warnings": warn}
