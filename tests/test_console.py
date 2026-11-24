"""Consoles: encrypted console password, console user per Proxmox server, Proxmox termproxy consoles (LXC via the API
token, VMs via a console user), the /api/console websocket with automatic login."""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import secretbox  # noqa: E402

# ---- secret box: Fernet, key file 0600 in the data folder
d = tempfile.mkdtemp()
tok = secretbox.seal(d, "Console-pw1!")
assert "Console-pw1!" not in tok and secretbox.unseal(d, tok) == "Console-pw1!"
kf = os.path.join(d, "console.key")
assert os.path.exists(kf) and stat.S_IMODE(os.stat(kf).st_mode) == 0o600
key = open(kf, "rb").read()
assert secretbox.unseal(d, secretbox.seal(d, "x")) == "x" and open(kf, "rb").read() == key      # same key reused
for bad_dir, bad_tok in ((tempfile.mkdtemp(), tok), (d, "garbage")):
    try:
        secretbox.unseal(bad_dir, bad_tok)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
print("OK  secretbox: versleuteld, sleutel 0600, hergebruikt, verkeerde sleutel/token -> ValueError")
