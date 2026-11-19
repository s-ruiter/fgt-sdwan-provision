"""Create the FortiManager / FortiGate VMs on Proxmox from the uploaded images, with a Day-0 config-drive ISO."""
import re
import tempfile
import uuid
from pathlib import Path

from . import cloudinit
from .pve import Pve, PveError

from .model import TAG_VM as TAG


def make_pve(cfg, token_secret):
    return Pve(cfg["url"], cfg["token_id"], token_secret, verify_tls=cfg.get("verify_tls", True))


def _port_no(name):
    m = re.fullmatch(r"port(\d+)", name or "")
    return int(m.group(1)) if m else None


MIN_FGT_NICS = 6          # every FortiGate-VM gets at least 6 interfaces (matches FMG vm_interface_number = 6)


def fgt_port_count(dev):
    """Number of ports (port1..portN) a FortiGate-VM of this device has: the highest portN it uses, at least 6."""
    used = [dev.get(k) for k in ("wan1_intf", "wan2_intf")] + [l.get("intf") for l in dev.get("lans", [])]
    return max([n for n in map(_port_no, used) if n] + [MIN_FGT_NICS])


def fgt_nics(dev, cfg, wanemu, is_hub, index=None):
    """One virtio NIC per port up to the highest portN the device uses, at least MIN_FGT_NICS. The wan ports go to the
    WAN emulator (hub wan1 -> ISP-HUB; branch wan1 -> ISP-A, wan2 -> ISP-B) with a fixed MAC (DHCP reservation); the
    other ports take bridge/VLAN from the per-port table."""
    from .wanemu.net import mac_for
    seg = wanemu["segments"]
    wans = {dev.get("wan1_intf"): "hub"} if is_hub else {dev.get("wan1_intf"): "a", dev.get("wan2_intf"): "b"}
    lans = {}                                                       # LAN1/LAN2 on their own VLAN per FortiGate
    if index is not None:
        for k, l in enumerate((dev.get("lans") or [])[:2]):
            if l.get("intf"):
                lans[l["intf"]] = wanemu["lan_vlan_base" if k == 0 else "lan2_vlan_base"] + 1 + index
    top = fgt_port_count(dev)
    ports = cfg.get("ports", {})
    nics = []
    for i in range(1, top + 1):
        name = f"port{i}"
        if name in lans and name not in wans:
            nics.append({"bridge": wanemu["bridge"], "vlan": lans[name]})
        elif name in wans:
            nics.append({"bridge": wanemu["bridge"], "vlan": seg[wans[name]]["vlan"], "mac": mac_for(dev["name"], i)})
        else:
            p = ports.get(name) or {}
            nics.append({"bridge": p.get("bridge") or cfg["default_bridge"], "vlan": p.get("vlan")})
    return nics


def build_specs(topo, admin_fgt_pw, admin_fmg_pw, tokens, fmg_token, which):
    """VM specs for `which` in ('fmg', 'fgt'); user_data (secret!) is computed here and never logged."""
    cfg, mode = topo["pve"], topo["pve"].get("license_mode", "cli")
    specs = []
    if which == "fmg":
        f = topo["fmgr"]
        fm = cfg["fmg"]
        cli = cloudinit.fmg_cli(topo, admin_fmg_pw)
        specs.append({"name": f.get("name") or "fmg", "role": "fmg", "cores": fm["cores"], "memory_mb": fm["memory_mb"],
                      "nics": [{"bridge": fm.get("bridge") or cfg["default_bridge"], "vlan": fm.get("vlan")}],
                      "extra_disks": [fm["data_disk_gb"]],
                      "user_data": cloudinit.build_user_data(cli, fmg_token, cloudinit.license_mode("fmg", mode))})
    else:
        fg = cfg["fgt"]
        hubs = {h["name"] for h in topo["hubs"]}
        for n, d in enumerate(topo["hubs"] + topo["edges"]):
            cli = cloudinit.fgt_cli(d, topo, admin_fgt_pw)
            specs.append({"name": d["name"], "role": "fgt", "cores": fg["cores"], "memory_mb": fg["memory_mb"],
                          "nics": fgt_nics(d, cfg, topo["wanemu"], d["name"] in hubs, n), "extra_disks": [],
                          "user_data": cloudinit.build_user_data(cli, tokens.get(d["name"]), cloudinit.license_mode("fgt", mode))})
    return specs


def vm_params(spec, cfg, import_volid, iso_volid):
    p = {"cpu": cfg.get("cpu_type", "host"), "cores": spec["cores"], "memory": spec["memory_mb"], "bios": "seabios",
         "machine": "pc", "ostype": "l26", "scsi0": f"{cfg['disk_storage']}:0,import-from={import_volid}",
         "boot": "order=scsi0", "start": 0, "tags": TAG}
    if spec.get("role") == "fgt":
        # Without scsihw the Proxmox API uses the LSI 53C895A controller; the FortiOS 8.0 kernel then finds no disk and
        # hangs silently after "Loading /rootfs.gz...ok" (seen on PVE 9.1 + FGT-VM 8.0.2). Fortinet's Proxmox guide uses
        # the GUI default, VirtIO SCSI single. (The FortiManager VM boots fine on LSI and is left as it is.)
        p["scsihw"] = "virtio-scsi-single"
    if iso_volid:
        p["ide2"] = f"{iso_volid},media=cdrom"
    for i, gb in enumerate(spec["extra_disks"], start=1):
        p[f"scsi{i}"] = f"{cfg['disk_storage']}:{gb}"
    for i, n in enumerate(spec["nics"]):
        v = (f"virtio={n['mac']}" if n.get("mac") else "virtio") + f",bridge={n['bridge']}"
        if n.get("vlan"):
            v += f",tag={n['vlan']}"
        p[f"net{i}"] = v
    return p


def plan_lines(spec):
    return (f"{spec['name']}: {spec['role']} {spec['cores']} vCPU / {spec['memory_mb']} MB, "
            f"{len(spec['nics'])} NIC(s)" + (f", datadisk {spec['extra_disks'][0]} GB" if spec["extra_disks"] else "")
            + ", Day-0 ISO")


def deploy_vms(pve, cfg, specs, image_paths, emit, step, state, start=True):
    """Upload image(s), build+upload a Day-0 ISO per VM, create VMs, start them. Idempotent by VM name.
    On failure everything this call created (VMs, uploaded volumes) is removed again."""
    node, job = cfg["node"], uuid.uuid4().hex[:8]
    created, uploaded = [], []

    def vol_delete(storage, volid):
        try:
            pve.delete_volume(node, storage, volid)
        except PveError:
            pass

    try:
        existing = {v["name"]: v for v in pve.find_vms(TAG)}
        todo = []
        for s in specs:
            if s["name"] in existing:
                v = existing[s["name"]]
                state[s["name"]] = {"vmid": int(v["vmid"]), "iso_volid": None}
                emit(step, "running", f"{s['name']}: exists (VMID {v['vmid']}), reused")
            else:
                todo.append(s)
        import_vol = {}
        for role in sorted({s["role"] for s in todo}):
            path = image_paths[role]
            fname = f"fgt-sdwan-{job}-{role}.qcow2"
            emit(step, "running", f"uploading image {Path(path).name} to {cfg['import_storage']}")
            last = {"p": -1.0}

            def prog(frac, bps, last=last):
                if frac - last["p"] >= 0.1 or frac >= 1.0:
                    last["p"] = frac
                    emit(step, "running", f"upload {frac * 100:.0f}% ({bps / 1e6:.1f} MB/s)")
            pve.wait_task(node, pve.upload(node, cfg["import_storage"], path, fname, "import", prog))
            import_vol[role] = f"{cfg['import_storage']}:import/{fname}"
            uploaded.append((cfg["import_storage"], import_vol[role]))

        pending = []
        for s in todo:
            iso_volid = None
            if s["user_data"] is not None:
                iso_name = f"fgt-sdwan-{s['name']}-{job}.iso"
                with tempfile.TemporaryDirectory() as td:
                    iso_path = Path(td) / iso_name
                    cloudinit.build_config_drive(s["user_data"], iso_path)
                    pve.wait_task(node, pve.upload(node, cfg["iso_storage"], iso_path, iso_name, "iso"))
                iso_volid = f"{cfg['iso_storage']}:iso/{iso_name}"
                uploaded.append((cfg["iso_storage"], iso_volid))
            vmid = pve.next_vmid()
            upid = pve.create_vm(node, vmid, s["name"], vm_params(s, cfg, import_vol[s["role"]], iso_volid))
            created.append(vmid)
            state[s["name"]] = {"vmid": vmid, "iso_volid": iso_volid}
            pending.append((s, vmid, upid))
            emit(step, "running", f"{s['name']}: VM {vmid} created, importing the disk")
        for s, vmid, upid in pending:
            pve.wait_task(node, upid)
        if start:
            for s in specs:
                vmid = state[s["name"]]["vmid"]
                if s in todo or any(v["name"] == s["name"] and v.get("status") != "running" for v in existing.values()):
                    pve.wait_task(node, pve.start_vm(node, vmid))
                    emit(step, "running", f"{s['name']}: started")
        for storage, volid in uploaded:
            if volid.endswith(".qcow2"):
                vol_delete(storage, volid)
        uploaded[:] = [u for u in uploaded if not u[1].endswith(".qcow2")]
    except Exception:
        for vmid in created:
            try:
                pve.delete_vm(node, vmid)
            except Exception:
                pass
        for storage, volid in uploaded:
            vol_delete(storage, volid)
        raise


def cleanup_isos(pve, cfg, names, state, emit, step):
    """Eject the Day-0 ISO and delete it from storage: it holds the admin password and the (single-use) token."""
    for n in names:
        s = state.get(n) or {}
        if not s.get("iso_volid"):
            continue
        try:
            pve.set_config(cfg["node"], s["vmid"], {"ide2": "none,media=cdrom"})
            pve.delete_volume(cfg["node"], cfg["iso_storage"], s["iso_volid"])
            s["iso_volid"] = None
            emit(step, "info", f"{n}: Day-0 ISO detached and deleted")
        except PveError as e:
            emit(step, "warn", f"{n}: cleaning up the Day-0 ISO failed ({e.detail}); delete it by hand: it contains a password/token")
