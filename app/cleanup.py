"""Clean up a deployment on Proxmox: every VM / container the tool created (Proxmox tag fgt-sdwan) and the tool's
leftover files (Day-0 ISOs and image imports named fgt-sdwan-*). The deploy container itself is never touched: it has
no tag, and it is also excluded by name (fgt-sdwan, or this host's own name) in case someone tagged it."""
import re
import socket

from .model import TAG_VM
from .pve import PveError

PROTECT = {"fgt-sdwan", socket.gethostname().split(".")[0]}
# the tool's own file names: Day-0 ISOs fgt-sdwan-<device>-<job>.iso, imports fgt-sdwan-<job>-<role>.qcow2
TOOL_FILE = re.compile(r"fgt-sdwan-(.+-[0-9a-f]{8}\.iso|[0-9a-f]{8}-(fgt|fmg)\.qcow2)")


def plan(pve, node):
    """[{kind: vm|ct|file, vmid?, name, status?, storage?}] in deletion order (VMs, containers, files)."""
    items = []
    for kind, rows in (("vm", pve.find_vms(TAG_VM)), ("ct", pve.find_cts(TAG_VM))):
        for r in sorted(rows, key=lambda r: int(r["vmid"])):
            if r.get("node", node) != node or r.get("name") in PROTECT:
                continue
            items.append({"kind": kind, "vmid": int(r["vmid"]), "name": r.get("name") or str(r["vmid"]),
                          "status": r.get("status", "")})
    opts = pve.node_options(node)
    for content, storages in (("iso", opts.get("iso_storages", [])), ("import", opts.get("import_storages", []))):
        for st in storages:
            if isinstance(st, dict) and st.get("shared"):     # shared storage: may belong to a VM on another node
                continue
            name = st["name"] if isinstance(st, dict) else st
            for v in pve.list_content(node, name, content):
                vol = v.get("volid", "")
                if TOOL_FILE.fullmatch(vol.split("/", 1)[-1]):
                    items.append({"kind": "file", "name": vol, "storage": name})
    return items


def run(pve, node, items, log, only=None):
    """Stop + delete every item (only those named in `only`, the overview the user saw, when given). One failure does
    not stop the rest; returns [{name, kind, ok, error?}]. The status is read again right before deleting."""
    out = []
    for it in items:
        if only is not None and it["name"] not in only:
            continue
        try:
            if it["kind"] == "vm":
                if pve.vm_status(node, it["vmid"]) == "running":
                    pve.wait_task(node, pve.stop_vm(node, it["vmid"]))
                pve.wait_task(node, pve.delete_vm(node, it["vmid"]))
                log(f"VM {it['vmid']} ({it['name']}) deleted")
            elif it["kind"] == "ct":
                if pve.ct_status(node, it["vmid"]) == "running":
                    pve.wait_task(node, pve.stop_ct(node, it["vmid"]))
                pve.wait_task(node, pve.delete_ct(node, it["vmid"]))
                log(f"CT {it['vmid']} ({it['name']}) deleted")
            else:
                pve.delete_volume(node, it["storage"], it["name"])
                log(f"{it['name']} deleted")
            out.append({"name": it["name"], "kind": it["kind"], "ok": True})
        except PveError as e:
            log(f"{it['name']}: {e}")
            out.append({"name": it["name"], "kind": it["kind"], "ok": False, "error": str(e)})
    return out
