"""Day-0 (cloud-init) for Fortinet VMs: CLI generation, user_data, config-drive ISO.

FortiOS reads `openstack/latest/user_data` from a config-drive ISO at first boot. With FortiFlex the licence is
applied by appending `execute vm-license <token>` as the last CLI line ('cli', default: this is the method the
user runs in production with Spawnpad). 'mime' is the multipart variant from Fortinet's Proxmox guide
(config.txt + license.txt with `LICENSE-TOKEN: <token>`), kept as an option only.
Secrets never leave this module unmasked except inside the ISO itself.
"""
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pycdlib

from .bootstrap import fgt_network_lines, fmg_network_lines

MODES = ("mime", "cli")
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")


def q(value):
    """Quote a CLI string value."""
    if "\n" in value or "\r" in value:
        raise ValueError("een wachtwoord mag geen regeleinde bevatten")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _admin(admin_pw, fmg):
    if not admin_pw:
        return []
    head = "config system admin user" if fmg else "config system admin"
    # FortiManager: the factory `admin` has rpc-permit none, so the JSON-RPC login the tool uses answers -11 "No permission"
    extra = ["set rpc-permit read-write"] if fmg else []
    return [head, 'edit "admin"', f"set password {q(admin_pw)}", *extra, "next", "end"]


def fgt_cli(dev, topo, admin_pw):
    lines = ["config system global", f"set hostname {dev['name']}", "end"]
    lines += _admin(admin_pw, fmg=False)
    lines += fgt_network_lines(dev, topo)
    return "\n".join(lines) + "\n"


def fmg_cli(topo, admin_pw):
    lines = fmg_network_lines(topo, ip_last=False) + _admin(admin_pw, fmg=True)
    return "\n".join(lines) + "\n"


def license_mode(role, cfg_mode="cli"):
    """How the FortiFlex token goes into the Day-0 config drive.
    FortiGate: always MIME (config.txt + license.txt 'LICENSE-TOKEN: <token>'), Fortinet's documented FortiFlex method:
    FortiOS 8.0.2 refuses `execute vm-license` from a cloud-init script ("Do not allow to do this operation via
    script", return code -37). FortiManager: the configured mode (default cli), proven on FMG 8.0.2."""
    return "mime" if role == "fgt" else cfg_mode


def build_user_data(cli, token, mode="cli"):
    if mode not in MODES:
        raise ValueError(f"onbekende licentiemodus '{mode}'")
    if token is not None and not TOKEN_RE.match(token):
        raise ValueError("een FortiFlex-token mag alleen letters en cijfers bevatten (10-64 tekens)")
    if token is None:
        data = cli
    elif mode == "cli":
        data = cli.rstrip("\n") + f"\nexecute vm-license {token}\n"
    else:
        msg = MIMEMultipart("mixed")
        for fname, body in (("config.txt", cli), ("license.txt", f"LICENSE-TOKEN: {token}\n")):
            part = MIMEText(body, "plain", "us-ascii")
            part.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(part)
        data = msg.as_string()
    try:
        data.encode("ascii")
    except UnicodeEncodeError as e:
        raise ValueError("Day-0 configuratie mag alleen ASCII bevatten") from e
    return data


def build_config_drive(user_data, dest):
    """OpenStack-style config drive: label config-2, /openstack/latest/user_data (+ empty /openstack/content)."""
    import io
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, rock_ridge="1.09", vol_ident="config-2")
    iso.add_directory("/OPENSTACK", rr_name="openstack")
    iso.add_directory("/OPENSTACK/CONTENT", rr_name="content")
    iso.add_directory("/OPENSTACK/LATEST", rr_name="latest")
    raw = user_data.encode("ascii")
    iso.add_fp(io.BytesIO(raw), len(raw), "/OPENSTACK/LATEST/USER_DATA.;1", rr_name="user_data")
    iso.write(str(dest))
    iso.close()


def read_config_drive(path):
    """Return (volume_label, user_data) from a config-drive ISO (used by tests / debugging)."""
    import io
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    try:
        buf = io.BytesIO()
        iso.get_file_from_iso_fp(buf, rr_path="/openstack/latest/user_data")
        return iso.pvd.volume_identifier.decode().strip(), buf.getvalue().decode()
    finally:
        iso.close()


def mask(text, *secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "********")
    return text
