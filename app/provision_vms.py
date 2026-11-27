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


def fgt_nics(dev, cfg, wanemu, is_hub, index=None, link_no=None):
    """One virtio NIC per port up to the highest portN the device uses, at least MIN_FGT_NICS. The wan ports go to the
    WAN emulator, each on the VLAN of its own link (hub n wan1 -> ISP-HUB vlan_base+n; spoke n wan1 -> ISP-A, wan2 ->
    ISP-B vlan_base+n) with a fixed MAC (DHCP reservation); the other ports take bridge/VLAN from the per-port table.
    index = position among all FortiGates (LAN VLANs), link_no = 1-based number of the hub / of the spoke."""
    from .wanemu.net import mac_for
    seg = wanemu["segments"]
    no = link_no or 1
    wans = ({dev.get("wan1_intf"): seg["hub"]["vlan_base"] + no} if is_hub else
            {dev.get("wan1_intf"): seg["a"]["vlan_base"] + no, dev.get("wan2_intf"): seg["b"]["vlan_base"] + no})
    wans.pop(None, None)
    wans.pop("", None)
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
            nics.append({"bridge": wanemu["bridge"], "vlan": wans[name], "mac": mac_for(dev["name"], i)})
        else:
            p = ports.get(name) or {}
            nics.append({"bridge": p.get("bridge") or cfg["default_bridge"], "vlan": p.get("vlan")})
    return nics


def wan_specs(topo):
    """Only what the WAN layout check needs per FortiGate: name, Proxmox name, NICs (no passwords, no user data)."""
    from .model import pve_name
    nh, out = len(topo["hubs"]), []
    for n, d in enumerate(topo["hubs"] + topo["edges"]):
        out.append({"name": d["name"], "pve_name": pve_name(topo, d["name"]), "role": "fgt",
                    "nics": fgt_nics(d, topo["pve"], topo["wanemu"], n < nh, n, n + 1 if n < nh else n - nh + 1)})
    return out


def check_wan_layout(pve, topo):
    """Run before anything is changed (also before the wan-emu): reused FortiGate VMs must have their WAN ports on the
    VLAN of their own link. Raises PveError."""
    _check_wan_layout(pve, topo["pve"]["node"], wan_specs(topo), {v["name"]: v for v in pve.find_vms(TAG)})


def _check_wan_layout(pve, node, specs, existing):
    """A FortiGate VM that is reused must already have its WAN ports on the VLAN of its own link. A reused VM keeps its
    own configuration (addresses, FMG link): moved to another VLAN it would lose FortiManager. This happens with an
    older deployment (one shared VLAN per ISP) and when the WAN numbering changes (a hub/spoke added, removed or moved,
    another VLAN base). Stop before changing anything."""
    for s in specs:
        if s.get("role") != "fgt":
            continue
        v = existing.get(s.get("pve_name") or s["name"]) or existing.get(s["name"])
        if not v:
            continue
        try:
            cfg = pve.vm_config(node, int(v["vmid"])) or {}
        except PveError:
            continue
        for i, nic in enumerate(s["nics"]):
            if not nic.get("mac"):                                  # only the WAN ports (fixed MAC)
                continue
            if f"net{i}" not in cfg:                              # no such NIC (yet): nothing to compare
                continue
            m = re.search(r"tag=(\d+)", str(cfg[f"net{i}"]))
            have = int(m.group(1)) if m else None
            if have != nic.get("vlan"):
                raise PveError(f"{v['name']} (VM {v['vmid']}): WAN port port{i + 1} is on VLAN {have}, this deployment "
                               f"needs VLAN {nic.get('vlan')} (one VLAN per WAN, numbered by the order of the hubs/spokes "
                               "and the VLAN base). It was deployed before with another WAN layout, or FortiGates were "
                               "added, removed or reordered: run Cleanup first, then deploy again")


def fmg_note(ip):
    """Proxmox notes of a FortiManager VM: the address its Day-0 config gave it (read back by check_fmg_reuse)."""
    return f"fgt-sdwan FortiManager ip={ip}"


def vm_note(role, ip="", serial=""):
    """Proxmox notes of a tool VM: FortiManager address (check_fmg_reuse) and the licence serial of the device (Lab
    start / stop take the licence from here, so each VM gets its own licence back). "" when there is nothing to note."""
    parts = []
    if role == "fmg" and ip:
        parts.append(fmg_note(ip))
    if serial:
        parts.append(f"licence={serial}")
    if not parts:
        return ""
    return " ".join(parts) if parts[0].startswith("fgt-sdwan") else "fgt-sdwan " + " ".join(parts)


def note_serial(note):
    m = re.search(r"\blicence=([A-Za-z0-9-]+)", str(note or ""))
    return m.group(1) if m else ""


def refresh_note(pve, node, vmid, spec):
    """A reused VM: rewrite its notes with this deployment's serial (and FortiManager address). Without a serial this
    time the one already in the notes is kept."""
    serial = spec.get("serial") or ""
    if not serial and hasattr(pve, "vm_config"):
        serial = note_serial((pve.vm_config(node, int(vmid)) or {}).get("description"))
    note = vm_note(spec.get("role"), spec.get("ip") or "", serial)
    if note:
        pve.set_config(node, int(vmid), {"description": note})


def check_fmg_reuse(pve, topo, known=None):
    """The FortiManager is known by its hostname (VM name and FortiFlex licence). A VM of that name is reused, and a
    reused VM keeps its own Day-0 config: with another IP the deployment would wait in vain. Stop before anything is
    touched. The address comes from the VM's notes; for an older VM without them from `known` ({hostname: address}
    remembered by the tool); unknown = reuse as before. Raises PveError."""
    from .model import pve_name, split_cidr
    name = topo["fmgr"].get("name") or "fmg"
    pn = pve_name(topo, name)
    vms = {v["name"]: v for v in pve.find_vms(TAG)}
    v = vms.get(pn) or vms.get(name)
    want = split_cidr(topo["fmgr"].get("ip") or "")[0]
    if not v or not want:
        return
    try:
        note = str((pve.vm_config(topo["pve"]["node"], int(v["vmid"])) or {}).get("description") or "")
    except PveError:
        return
    m = re.search(r"fgt-sdwan FortiManager ip=([0-9.]+)", note)
    have = m.group(1) if m else (known or {}).get(name)
    if have and have != want:
        raise PveError(f"{v['name']} (VM {v['vmid']}) is a FortiManager with IP {have}; this deployment wants {want} for "
                       f"the same name, and would reuse that VM. For a second FortiManager give it its own hostname "
                       f"(FortiManager card, e.g. {name}2): it gets its own VM and licence. To redeploy this one, keep "
                       f"IP {have}, or run Cleanup first.")


def deploy_names(t, scope, fmg_vm=None, fgt_vm=None):
    """Names of everything a deployment creates on Proxmox, in the order of its id block: FortiManager, wan-emu,
    hubs, spokes, LAN clients. fmg_vm / fgt_vm default to what the scope deploys (the pipeline passes the methods)."""
    from .model import pve_name
    fmg_vm = scope in ("all", "fmg") if fmg_vm is None else fmg_vm
    fgt_vm = scope in ("all", "fgt") if fgt_vm is None else fgt_vm
    out = [t["fmgr"].get("name") or "fmg"] if fmg_vm else []
    if fgt_vm:
        if not t["wanemu"].get("vmid"):                   # a fixed wan-emu id stays where it is
            out.append(t["wanemu"]["hostname"])
        out += [d["name"] for d in t["hubs"] + t["edges"]]
    if fgt_vm or scope == "lan":
        out += [f"client-{e['name']}" for e in t["edges"] if (e.get("lans") or [{}])[0].get("ip")]
        if (t.get("trafficgen") or {}).get("enabled"):
            out.append("trafficgen")
    return [pve_name(t, n) for n in out]


def plan_vmids(pve, names, first=None, prefix=""):
    """{name: vmid} for the names that do not exist yet (tool VM/CT with that name, or with that name without the
    prefix: reused, keeps its id): the first free block of consecutive ids from `first` (or 100), in the order given."""
    have = set()
    for r in pve.find_vms(TAG) + pve.find_cts(TAG):
        have |= {r.get("name"), prefix + str(r.get("name"))}
    new = [n for n in names if n not in have]
    if not new:
        return {}
    used, s = pve.used_vmids(), max(100, int(first or 100))
    while True:
        clash = [s + i for i in range(len(new)) if s + i in used]
        if not clash:
            return {n: s + i for i, n in enumerate(new)}
        s = max(clash) + 1


def pick_vmid(pve, vmids, name):
    """The planned id for `name`, unless something took it in the meantime: then the next free one."""
    v = (vmids or {}).get(name)
    if v and v not in pve.used_vmids():
        return v
    return pve.next_vmid()


def build_specs(topo, admin_fgt_pw, admin_fmg_pw, tokens, fmg_token, which, licenses=None):
    """VM specs for `which` in ('fmg', 'fgt'); user_data (secret!) is computed here and never logged. licenses =
    {device: licence file bytes} (licence-file mode): that device gets the file on its config drive instead of a token."""
    licenses = licenses or {}
    from .model import pve_name
    cfg, mode = topo["pve"], topo["pve"].get("license_mode", "cli")
    specs = []
    if which == "fmg":
        f = topo["fmgr"]
        fm = cfg["fmg"]
        cli = cloudinit.fmg_cli(topo, admin_fmg_pw)
        lic = licenses.get(f.get("name") or "fmg")
        specs.append({"name": f.get("name") or "fmg", "pve_name": pve_name(topo, f.get("name") or "fmg"), "role": "fmg", "cores": fm["cores"], "memory_mb": fm["memory_mb"],
                      "nics": [{"bridge": fm.get("bridge") or cfg["default_bridge"], "vlan": fm.get("vlan")}],
                      "extra_disks": [fm["data_disk_gb"]], "license": lic, "ip": f.get("ip") or "", "serial": (f.get("flex_serial") or f.get("serial") or "").strip(),
                      "user_data": cloudinit.build_user_data(cli, None if lic else fmg_token, cloudinit.license_mode("fmg", mode))})
    else:
        fg = cfg["fgt"]
        hubs = {h["name"] for h in topo["hubs"]}
        nh = len(topo["hubs"])
        for n, d in enumerate(topo["hubs"] + topo["edges"]):
            cli = cloudinit.fgt_cli(d, topo, admin_fgt_pw)
            specs.append({"name": d["name"], "pve_name": pve_name(topo, d["name"]), "role": "fgt", "cores": fg["cores"], "memory_mb": fg["memory_mb"],
                          "nics": fgt_nics(d, cfg, topo["wanemu"], d["name"] in hubs, n, n + 1 if n < nh else n - nh + 1),
                          "extra_disks": [int(fg.get("log_disk_gb") or 10)], "license": licenses.get(d["name"]), "serial": (d.get("serial") or "").strip(),
                          "user_data": cloudinit.build_user_data(cli, None if licenses.get(d["name"]) else tokens.get(d["name"]),
                                                                 cloudinit.license_mode("fgt", mode))})
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
    # serial console: the tool's console (Proxmox termproxy needs serial0) and a text console with paste in the
    # Proxmox GUI; FortiOS and FortiManager both log in on serial0 (tested: 'site1-3 login:', 'fmg login:')
    p["serial0"], p["vga"] = "socket", "serial0"
    note = vm_note(spec.get("role"), spec.get("ip") or "", spec.get("serial") or "")
    if note:
        p["description"] = note
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
            f"{len(spec['nics'])} NIC(s)" + (f", {'log' if spec.get('role') == 'fgt' else 'data'} disk {spec['extra_disks'][0]} GB" if spec["extra_disks"] else "")
            + ", Day-0 ISO")


def deploy_vms(pve, cfg, specs, image_paths, emit, step, state, start=True, vmids=None):
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
        _check_wan_layout(pve, node, specs, existing)              # before anything is changed
        todo = []
        for s in specs:
            pn = s.get("pve_name") or s["name"]
            if pn not in existing and s["name"] in existing:          # deployed before without (this) prefix: rename
                v = existing.pop(s["name"])
                pve.set_config(node, int(v["vmid"]), {"name": pn})
                existing[pn] = dict(v, name=pn)
                emit(step, "running", f"{s['name']}: VM {v['vmid']} renamed to {pn}")
            if pn in existing:
                v = existing[pn]
                state[s["name"]] = {"vmid": int(v["vmid"]), "iso_volid": None}
                emit(step, "running", f"{pn}: exists (VMID {v['vmid']}), reused")
                try:                                          # this deployment's licence serial in its notes
                    refresh_note(pve, node, v["vmid"], s)
                except PveError as e:
                    emit(step, "warn", f"{pn}: notes not updated ({e})")
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
                    cloudinit.build_config_drive(s["user_data"], iso_path, license=s.get("license"))
                    pve.wait_task(node, pve.upload(node, cfg["iso_storage"], iso_path, iso_name, "iso"))
                iso_volid = f"{cfg['iso_storage']}:iso/{iso_name}"
                uploaded.append((cfg["iso_storage"], iso_volid))
            pn = s.get("pve_name") or s["name"]
            vmid = pick_vmid(pve, vmids, pn)
            upid = pve.create_vm(node, vmid, pn, vm_params(s, cfg, import_vol[s["role"]], iso_volid))
            created.append(vmid)
            state[s["name"]] = {"vmid": vmid, "iso_volid": iso_volid}
            pending.append((s, vmid, upid))
            emit(step, "running", f"{s['name']}: VM {vmid} created, importing the disk")
        for s, vmid, upid in pending:
            pve.wait_task(node, upid)
        if start:
            for s in specs:
                vmid = state[s["name"]]["vmid"]
                if s in todo or any(v["name"] == (s.get("pve_name") or s["name"]) and v.get("status") != "running"
                                    for v in existing.values()):
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
