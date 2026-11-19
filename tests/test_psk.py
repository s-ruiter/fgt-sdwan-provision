"""Random per-ADOM IPsec PSK + fixed settings.  python -m tests.test_psk"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, model

c = db.connect(tempfile.mkdtemp())
assert db.get_psk(c, "A") == db.PSK_PLACEHOLDER                    # preview: nothing generated
assert c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'psk:%'").fetchone()[0] == 0
a1 = db.get_psk(c, "A", create=True)
assert len(a1) >= 40 and a1 != db.PSK_PLACEHOLDER
assert db.get_psk(c, "A", create=True) == a1 == db.get_psk(c, "A")   # stable per ADOM
b1 = db.get_psk(c, "B", create=True)
assert b1 != a1                                                      # different per ADOM, random
assert len({db.get_psk(db.connect(tempfile.mkdtemp()), "A", create=True) for _ in range(20)}) == 20
mode = oct(os.stat(os.path.join(os.path.dirname(c.execute("PRAGMA database_list").fetchone()[2]), "fgt-sdwan.db")).st_mode & 0o777)
assert mode == "0o600", mode
t = model.merged({})
assert (t["dns_primary"], t["dns_secondary"], t["wan1_access"], t["platform"]) == ("1.1.1.1", "8.8.8.8", "ping https ssh fgfm", "FortiGate-VM64")
assert (t["region"]["name"], t["region"]["bgp_as"], t["region"]["lo_summary"], t["lo_summary"]) == ("West", 65001, "10.200.1.0/24", "10.200.0.0/14")
print("OK  PSK: willekeurig, stabiel per ADOM, placeholder in previews, db 0600; vaste instellingen")

# last-used per Proxmox server also remembers the WAN emulator uplink (it has no default); an empty one is not saved
c2 = db.connect(tempfile.mkdtemp())
for i in (1, 2):
    c2.execute("INSERT INTO servers(id,name,url,token_id,token_secret) VALUES(?,?,?,?,?)", (i, f"pve{i}", "https://x:8006", "a@pam!b", "s"))
up = {"bridge": "vmbr0", "vlan": 106, "ip": "10.10.106.250/24", "gw": "10.10.106.1"}
db.save_last_used(c2, 1, {"node": "pve01"}, {"uplink": up, "bridge": "vmbr5"})
assert db.get_last_used(c2, 1) == {"node": "pve01", "wanemu_uplink": up, "wanemu_bridge": "vmbr5"}, db.get_last_used(c2, 1)
db.save_last_used(c2, 2, {"node": "pve02"}, {"uplink": {"vlan": None, "ip": "", "gw": ""}})
assert db.get_last_used(c2, 2) == {"node": "pve02"}
db.save_last_used(c2, 1, {"node": "pve01"})                       # a run without wan-emu keeps the remembered uplink
assert db.get_last_used(c2, 1)["wanemu_uplink"] == up and db.get_last_used(c2, 1)["wanemu_bridge"] == "vmbr5"
print("OK  last-used: WAN-emulator uplink per server onthouden")
