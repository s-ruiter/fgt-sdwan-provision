"""Unit tests: image upload/extract, Day-0 cloud-init (MIME + ISO), Proxmox client against the fake API.
Run: python tests/test_day0_units.py"""
import email
import io
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cloudinit, images, model
from app.pve import Pve, PveError
from tests import fake_proxmox

TOKEN = "A1B2C3D4E5F6G7H8I9J0"


def fake_qcow2(gb=2):
    head = b"QFI\xfb" + struct.pack(">I", 3) + b"\0" * 16 + struct.pack(">Q", gb * 1024 ** 3)
    return head + b"\0" * 4096


def make_zip(name, inner):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(inner, fake_qcow2())
        z.writestr("README.txt", "x")
    return buf.getvalue()


def topo():
    t = model.merged({})
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    for i, d in enumerate(t["hubs"] + t["edges"]):
        d["serial"] = f"FGVMMLTM2600{i:04d}"
    t["hubs"][0]["wan1_gw"], t["hubs"][1]["wan1_gw"] = "100.64.1.6", "100.64.2.6"
    return t


def test_images():
    with tempfile.TemporaryDirectory() as td:
        z = make_zip("x", "fortios.qcow2")
        meta = images.save_upload(td, io.BytesIO(z), "FGT_VM64_KVM-v8.0.0-build0123-FORTINET.out.kvm.zip", "fgt")
        import hashlib
        assert meta["sha256"] == hashlib.sha256(z).hexdigest() and meta["virtual_gb"] == 2
        assert meta["version"] == "8.0.0" and meta["build"] == "0123" and meta["qcow2"] == "fortios.qcow2"
        got = images.get_image(td, meta["id"])
        assert Path(got["path"]).exists() and images.qcow2_virtual_size(got["path"]) == 2 * 1024 ** 3
        assert not list(Path(got["path"]).parent.glob("*.zip")), "zip moet na uitpakken weg zijn"
        for args, msg in ((("fmg", "FGT_VM64_KVM-v8.0.0-build1.zip"), "FortiGate-image"),
                          (("fgt", "notes.txt"), "alleen .zip"),):
            try:
                images.save_upload(td, io.BytesIO(z), args[1], args[0])
                raise AssertionError("moest falen")
            except images.ImageError as e:
                assert msg in str(e), e
        try:
            images.save_upload(td, io.BytesIO(b"not a qcow2" * 10), "fmg.qcow2", "fmg")
            raise AssertionError("moest falen")
        except images.ImageError as e:
            assert "geen geldig qcow2" in str(e)
        assert len(images.list_images(td)) == 1, "mislukte uploads mogen niets achterlaten"
        m2 = images.save_upload(td, io.BytesIO(make_zip("x", "fmg.qcow2")), "FMG_VM64_KVM-v7.6.1-build1.out.kvm.zip", "fmg")
        assert m2["warnings"] and "8.0.x" in m2["warnings"][0]
        images.delete_image(td, meta["id"])
        assert len(images.list_images(td)) == 1
    print("OK  images: zip uitpakken, sha256, versie, rol-controle, ongeldige qcow2/zip, versie-waarschuwing, delete")


def test_cloudinit():
    t = topo()
    hub, edge = t["hubs"][0], t["edges"][0]
    cli = cloudinit.fgt_cli(hub, t, 'p"w\\d')
    assert 'set password "p\\"w\\\\d"' in cli and f"set hostname {hub['name']}" in cli
    assert "set allowaccess ping https ssh fgfm" in cli and 'set fmg "10.0.0.5"' in cli and "set mode static" in cli
    assert cloudinit.fgt_cli(edge, t, "x")  # dhcp variant renders
    assert "set mode dhcp" in cloudinit.fgt_cli(edge, t, "x")
    for bad in ("short", "abc; config system admin", "AAAAAAAAAA\nexecute shutdown", "A" * 65, ""):
        try:
            cloudinit.build_user_data(cli, bad)
            raise AssertionError("token moest geweigerd worden: " + repr(bad))
        except ValueError:
            pass
    try:
        cloudinit.fgt_cli(hub, t, "a\nb")
        raise AssertionError("newline in wachtwoord")
    except ValueError:
        pass
    ud = cloudinit.build_user_data(cli, TOKEN, "mime")
    msg = email.message_from_string(ud)
    parts = {p.get_filename(): p.get_payload() for p in msg.get_payload()}
    assert msg.get_content_type() == "multipart/mixed" and set(parts) == {"config.txt", "license.txt"}
    assert parts["license.txt"].strip() == f"LICENSE-TOKEN: {TOKEN}" and parts["config.txt"] == cli
    assert f"execute vm-license {TOKEN}" in cloudinit.build_user_data(cli, TOKEN, "cli")
    assert cloudinit.build_user_data(cli, None, "mime") == cli
    with tempfile.TemporaryDirectory() as td:
        iso = Path(td) / "d.iso"
        cloudinit.build_config_drive(ud, iso)
        label, back = cloudinit.read_config_drive(iso)
        assert label == "config-2" and back == ud
    masked = cloudinit.mask(ud, TOKEN, 'p"w\\d')
    assert TOKEN not in masked and "********" in masked
    fm = cloudinit.fmg_cli(t, "Adm1n!")
    assert "set ip 10.0.0.5 255.255.255.0" in fm and "config system admin user" in fm and "set gateway 10.0.0.1" in fm
    assert cloudinit.build_user_data(cli, TOKEN).rstrip().endswith(f"execute vm-license {TOKEN}"), "default moet cli zijn"
    assert model.DEFAULTS["pve"]["license_mode"] == "cli"
    # FortiManager Day-0: port1 forced to static (a new FMG VM can sit on DHCP and then ignores `set ip` alone); interface
    # before the route so the gateway is inside the port's subnet. The SSH path keeps the address change last (session drops).
    t2 = model.merged({})
    t2["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1", name="fmg")
    d0_probe = cloudinit.fmg_cli(t2, "Pw-123456")
    d0 = d0_probe.splitlines()
    assert d0.count("set mode static") == 1 and d0.index("set mode static") < d0.index("set ip 10.10.106.11 255.255.255.0"), d0
    assert d0.index("config system interface") < d0.index("config system route") and "set gateway 10.10.106.1" in d0, d0
    # FortiManager Day-0: port1 forced to static (a new FMG VM can sit on DHCP and then ignores `set ip` alone); interface
    # before the route so the gateway is inside the port's subnet. The SSH path keeps the address change last (session drops).
    t2 = model.merged({})
    t2["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1", name="fmg")
    d0_probe = cloudinit.fmg_cli(t2, "Pw-123456")
    d0 = d0_probe.splitlines()
    assert d0.count("set mode static") == 1 and d0.index("set mode static") < d0.index("set ip 10.10.106.11 255.255.255.0"), d0
    assert d0.index("config system interface") < d0.index("config system route") and "set gateway 10.10.106.1" in d0, d0
    # FortiManager admin needs rpc-permit read-write (factory: none -> JSON-RPC login answers -11); FortiGates must not get it
    assert "set rpc-permit read-write" in d0_probe and "set rpc-permit" not in cloudinit.fgt_cli(t2["hubs"][0], t2, "Pw-123456")
    from app import bootstrap
    ssh = bootstrap.fmg_network_lines(t2)
    assert "set mode static" in ssh and ssh.index("config system route") < ssh.index("config system interface"), ssh
    assert ssh.index("set mode static") < ssh.index("set ip 10.10.106.11 255.255.255.0")
    assert bootstrap.fmg_api_lines("apiadmin") == ["config system admin user", 'edit "apiadmin"', "set rpc-permit read-write", "next", "end"]
    print("OK  cloud-init: cli-default (execute vm-license als laatste regel), MIME optioneel (config.txt + LICENSE-TOKEN), ISO label config-2 round-trip, quoting, token-validatie, masking, FMG-CLI")


def test_pve_client():
    fake_proxmox.reset()
    srv = fake_proxmox.start(8097)
    try:
        p = Pve("http://127.0.0.1:8097", "root@pam!t", "secret")
        c = p.check()
        assert c["version"] == "9.0.3" and c["import_storages"] == ["local"] and c["iso_storages"] == ["local"]
        assert c["image_storages"] == ["local-lvm"] and p.bridges("pve1") == ["vmbr0", "vmbr1"]
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.iso"
            data = os.urandom(3 * 1024 * 1024 + 17)
            f.write_bytes(data)
            seen = []
            p.wait_task("pve1", p.upload("pve1", "local", f, "a.iso", "iso", lambda fr, bps: seen.append(fr)))
            assert fake_proxmox.STATE["uploads"]["local:iso/a.iso"] == data, "upload moet byte-voor-byte aankomen"
            assert seen and seen[-1] == 1.0
        bad = Pve("http://127.0.0.1:8097", "root@pam!t", "secret")
        try:
            bad._req("GET", "/does/not/exist")
            raise AssertionError
        except PveError as e:
            assert e.status == 501
        p.delete_volume("pve1", "local", "local:iso/a.iso")
        assert "local:iso/a.iso" not in fake_proxmox.STATE["uploads"]
    finally:
        srv.shutdown()
    print("OK  Proxmox-client: check (PVE 9, import/iso/images storages), 3 MB streaming upload byte-voor-byte, delete, fouten")


if __name__ == "__main__":
    test_images()
    test_cloudinit()
    test_pve_client()
    print("alle day-0 unit-tests geslaagd")
