"""Licence files (FortiCare .lic, named after the serial number) as an alternative to FortiFlex.

Stored per device in <DATA_DIR>/licenses/<device>.lic (dir 0700, files 0600) with metadata in licenses.json; kept
across deployments (a deployment only reads them). The mode (fortiflex | files) is one tool setting; switching it
never touches the FortiFlex settings."""
import json
import os
import re
import time

from . import db

FGT_RE = re.compile(r"^FGVM[A-Z0-9]{8,16}$")
FMG_RE = re.compile(r"^(FMG-?VM|FMVM)[A-Z0-9]{6,16}$")       # FMG-VM… (FortiCare) and FMVM… (VM serial)
KIND = {"fgt": "FortiGate", "fmg": "FortiManager"}
MODES = ("fortiflex", "files")
DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")


def serial_from_name(filename):
    """('fgt'|'fmg', serial) from a FortiCare licence file name like FGVM08TM24001234.lic."""
    base = os.path.basename(str(filename or ""))
    stem, ext = os.path.splitext(base)
    s = stem.upper()
    if ext.lower() == ".lic":
        if FGT_RE.match(s):
            return "fgt", s
        if FMG_RE.match(s):
            return "fmg", s
    raise ValueError(f"{base or 'file'}: not a FortiCare licence file named after its serial number "
                     "(e.g. FGVM08TM24001234.lic or FMG-VMTM24005678.lic)")


def _dir(data_dir):
    d = os.path.join(data_dir, "licenses")
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _meta_path(data_dir):
    return os.path.join(_dir(data_dir), "licenses.json")


def listing(data_dir):
    try:
        with open(_meta_path(data_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_meta(data_dir, meta):
    p = _meta_path(data_dir)
    with open(p + ".tmp", "w") as f:
        json.dump(meta, f, indent=1)
    os.replace(p + ".tmp", p)
    os.chmod(p, 0o600)


def save(data_dir, device, kind, filename, content):
    """Store the licence file of one device (replaces its earlier one). Raises ValueError for a wrong name/kind/duplicate."""
    if not DEVICE_RE.match(str(device or "")):
        raise ValueError("invalid device name")
    k, serial = serial_from_name(filename)
    if k != kind:
        raise ValueError(f"{os.path.basename(filename)} is a {KIND[k]} licence, {device} needs a {KIND[kind]} licence")
    if not content:
        raise ValueError(f"{os.path.basename(filename)} is empty")
    meta = listing(data_dir)
    for other, m in meta.items():
        if other != device and m.get("serial") == serial:
            raise ValueError(f"serial {serial} is already used for {other}")
    p = os.path.join(_dir(data_dir), f"{device}.lic")
    fd = os.open(p + ".tmp", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    os.replace(p + ".tmp", p)
    os.chmod(p, 0o600)
    meta[device] = {"serial": serial, "file": os.path.basename(filename), "size": len(content),
                    "uploaded": time.strftime("%Y-%m-%d %H:%M:%S")}
    _write_meta(data_dir, meta)
    return meta[device]


def read(data_dir, device):
    if device not in listing(data_dir):
        return None
    try:
        with open(os.path.join(_dir(data_dir), f"{device}.lic"), "rb") as f:
            return f.read()
    except OSError:
        return None


def delete(data_dir, device):
    meta = listing(data_dir)
    if device not in meta:
        return False
    try:
        os.remove(os.path.join(_dir(data_dir), f"{device}.lic"))
    except OSError:
        pass
    meta.pop(device)
    _write_meta(data_dir, meta)
    return True


def get_mode(conn):
    v = db.get_secret(conn, "licensing")
    return v if v in MODES else "fortiflex"


def set_mode(conn, mode):
    if mode not in MODES:
        raise ValueError(f"licensing mode must be one of {', '.join(MODES)}")
    db.put_secret(conn, "licensing", mode)
