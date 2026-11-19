"""Uploaded Fortinet KVM images (FortiGate / FortiManager), stored in the deploy container.

The user downloads `FGT_VM64_KVM-*.out.kvm.zip` / `FMG_VM64_KVM-*.out.kvm.zip` from the FortiCloud portal and
uploads them here; the qcow2 inside is extracted and later pushed to Proxmox by app/provision_vms.py.
"""
import hashlib
import json
import math
import re
import shutil
import struct
import time
import uuid
import zipfile
from pathlib import Path

CHUNK = 1024 * 1024
MAGIC = b"QFI\xfb"
ID_RE = re.compile(r"^[0-9a-f]{12}$")
ROLES = {"fgt": "FortiGate", "fmg": "FortiManager"}
_HINT = {"fgt": re.compile(r"(?i)fgt|fortios|fortigate"), "fmg": re.compile(r"(?i)fmg|fortimanager")}
_VER = re.compile(r"(?i)v(\d+\.\d+(?:\.\d+)?)[-_.]?build(\d+)")


class ImageError(ValueError):
    pass


def images_dir(data_dir):
    d = Path(data_dir) / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def qcow2_virtual_size(path):
    with open(path, "rb") as fh:
        head = fh.read(32)
    if len(head) < 32 or head[:4] != MAGIC:
        raise ImageError(f"{Path(path).name} is not a valid qcow2 file")
    return struct.unpack(">Q", head[24:32])[0]


def virtual_gb(path):
    return math.ceil(qcow2_virtual_size(path) / 1024 ** 3)


def _pick_qcow2(zip_path, dest, role):
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ImageError("invalid zip file") from e
    out = []
    with zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith(".qcow2"):
                continue
            target = dest / name
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
            except (zipfile.BadZipFile, OSError) as e:
                raise ImageError("zip file is damaged") from e
            out.append(target)
    if not out:
        raise ImageError("no .qcow2 found in the zip")
    for p in out:                      # prefer the file that matches the role (fortios.qcow2 / fmg.qcow2)
        if _HINT[role].search(p.name):
            return p
    return out[0]


def save_upload(data_dir, fileobj, filename, role):
    """Stream an upload to disk, verify and register it. Returns the metadata dict."""
    if role not in ROLES:
        raise ImageError("role must be 'fgt' or 'fmg'")
    name = Path(filename or "upload").name
    low = name.lower()
    if not low.endswith((".zip", ".qcow2")):
        raise ImageError("only .zip or .qcow2 allowed")
    other = "fmg" if role == "fgt" else "fgt"
    if _HINT[other].search(name) and not _HINT[role].search(name):
        raise ImageError(f"'{name}' looks like a {ROLES[other]} image, but was uploaded as {ROLES[role]}")
    cleanup_stale(data_dir)
    iid = uuid.uuid4().hex[:12]
    d = images_dir(data_dir) / iid
    d.mkdir()
    try:
        raw = d / name
        h = hashlib.sha256()
        with open(raw, "wb") as fh:
            while True:
                chunk = fileobj.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                fh.write(chunk)
        size = raw.stat().st_size
        if low.endswith(".zip"):
            q = _pick_qcow2(raw, d, role)
            raw.unlink()
        else:
            q = raw
        vs = qcow2_virtual_size(q)
        m = _VER.search(name)
        meta = {"id": iid, "role": role, "filename": name, "qcow2": q.name, "upload_bytes": size,
                "sha256": h.hexdigest(), "virtual_gb": math.ceil(vs / 1024 ** 3),
                "version": m.group(1) if m else None, "build": m.group(2) if m else None,
                "uploaded_at": time.time(), "warnings": []}
        if role == "fmg" and meta["version"] and not meta["version"].startswith("8.0"):
            meta["warnings"].append(f"FortiManager image is v{meta['version']}, the tool expects 8.0.x")
        (d / "meta.json").write_text(json.dumps(meta))
        return meta
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def list_images(data_dir):
    out = []
    for p in sorted(images_dir(data_dir).glob("*/meta.json")):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def get_image(data_dir, iid):
    if not ID_RE.match(iid or ""):
        raise ImageError("unknown image")
    p = images_dir(data_dir) / iid / "meta.json"
    if not p.exists():
        raise ImageError("unknown image")
    meta = json.loads(p.read_text())
    meta["path"] = str(p.parent / meta["qcow2"])
    return meta


def delete_image(data_dir, iid):
    if not ID_RE.match(iid or ""):
        raise ImageError("unknown image")
    shutil.rmtree(images_dir(data_dir) / iid, ignore_errors=True)


def cleanup_stale(data_dir, max_age_s=6 * 3600):
    """Remove half-finished uploads (directory without meta.json) older than max_age_s."""
    cutoff = time.time() - max_age_s
    for d in images_dir(data_dir).iterdir():
        if d.is_dir() and not (d / "meta.json").exists() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
