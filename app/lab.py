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
