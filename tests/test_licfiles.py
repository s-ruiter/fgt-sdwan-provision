"""Licence files (FortiCare .lic, named after the serial) as an alternative to FortiFlex."""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import db, licfiles as lf  # noqa: E402

assert lf.serial_from_name("FGVM08TM24001234.lic") == ("fgt", "FGVM08TM24001234")
assert lf.serial_from_name("fgvm08tm24001234.LIC") == ("fgt", "FGVM08TM24001234")
assert lf.serial_from_name("FMG-VMTM24005678.lic") == ("fmg", "FMG-VMTM24005678")
assert lf.serial_from_name("FMGVMTM24005678.lic") == ("fmg", "FMGVMTM24005678")
for bad in ("foo.lic", "FGVM08TM24001234.txt", "license.lic"):
    try:
        lf.serial_from_name(bad)
        raise AssertionError(bad)
    except ValueError:
        pass
d = tempfile.mkdtemp()
m = lf.save(d, "site1-H1", "fgt", "FGVM08TM24001234.lic", b"-----BEGIN FGT VM LICENSE-----\nabc\n")
assert m["serial"] == "FGVM08TM24001234" and m["file"] == "FGVM08TM24001234.lic" and m["size"] > 0
p = os.path.join(d, "licenses", "site1-H1.lic")
assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode) == 0o700
assert lf.read(d, "site1-H1").startswith(b"-----BEGIN") and lf.read(d, "nope") is None
for dev, kind, name, why in (("fmg", "fmg", "FGVM08TM24009999.lic", "FortiGate"),       # FortiGate file on the FMG row
                             ("site1-H2", "fgt", "FMG-VMTM24005678.lic", "FortiManager"),
                             ("site1-H2", "fgt", "FGVM08TM24001234.lic", "site1-H1")):    # serial already used
    try:
        lf.save(d, dev, kind, name, b"x")
        raise AssertionError((dev, name))
    except ValueError as e:
        assert why in str(e), (why, str(e))
lf.save(d, "site1-H1", "fgt", "FGVM08TM24001234.lic", b"new")                     # replace on the same device: ok
assert lf.read(d, "site1-H1") == b"new" and list(lf.listing(d)) == ["site1-H1"]
assert lf.delete(d, "site1-H1") and not lf.delete(d, "site1-H1") and lf.listing(d) == {}
print("OK  licentiebestanden: serienummer uit de naam, soort gecontroleerd, uniek, 0600/0700, vervangen/verwijderen")

conn = db.connect(os.environ["DATA_DIR"])
db.put_secret(conn, "fortiflex", '{"user": "u"}')
assert lf.get_mode(conn) == "fortiflex"
lf.set_mode(conn, "files")
assert lf.get_mode(conn) == "files" and db.get_secret(conn, "fortiflex") == '{"user": "u"}'
try:
    lf.set_mode(conn, "other")
    raise AssertionError("bad mode")
except ValueError:
    pass
lf.set_mode(conn, "fortiflex")
print("OK  modus: standaard FortiFlex, wisselen laat de FortiFlex-instellingen staan")

# ---- Day-0 config drive carries the licence file (openstack/content/0000 + meta_data.json files entry)
import io  # noqa: E402
import json  # noqa: E402

import pycdlib  # noqa: E402

from app import cloudinit, model, provision_vms as pv  # noqa: E402
from tests import labuplink  # noqa: E402,F401


def iso_file(path, rr):
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    buf = io.BytesIO()
    try:
        iso.get_file_from_iso_fp(buf, rr_path=rr)
    except Exception:
        return None
    finally:
        iso.close()
    return buf.getvalue()


lic = b"-----BEGIN FGT VM LICENSE-----\nQUJD\n-----END FGT VM LICENSE-----\n"
p = os.path.join(tempfile.mkdtemp(), "d.iso")
cloudinit.build_config_drive("#cfg\n", p, license=lic)
assert iso_file(p, "/openstack/content/0000") == lic
assert json.loads(iso_file(p, "/openstack/latest/meta_data.json")) == {"files": [{"path": "license", "content_path": "/content/0000"}]}
assert iso_file(p, "/openstack/latest/user_data") == b"#cfg\n"
p2 = os.path.join(tempfile.mkdtemp(), "e.iso")
cloudinit.build_config_drive("#cfg\n", p2)
assert iso_file(p2, "/openstack/latest/meta_data.json") is None and iso_file(p2, "/openstack/content/0000") is None
t = model.merged({})
t["fmgr"].update(ip="10.10.106.11/24", gateway="10.10.106.1")
lics = {"site1-H1": lic, "fmg": b"-----BEGIN FMG VM LICENSE-----\nx\n"}
fg = {s["name"]: s for s in pv.build_specs(t, "Pw-12345!", "Pw-12345!", {}, None, "fgt", licenses=lics)}
assert fg["site1-H1"]["license"] == lic and "LICENSE-TOKEN" not in fg["site1-H1"]["user_data"] and fg["site1-1"].get("license") is None
fm = pv.build_specs(t, "Pw-12345!", "Pw-12345!", {}, None, "fmg", licenses=lics)[0]
assert fm["license"] == lics["fmg"] and "vm-license" not in fm["user_data"]
print("OK  Day-0: licentiebestand op de config drive (content/0000 + meta_data.json), geen FortiFlex-token in user_data")
