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
