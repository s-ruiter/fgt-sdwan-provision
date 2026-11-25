"""Probe a real FortiManager: where are the internet services (ISDB) the demo SD-WAN rules match on, and are they there?
Read-only: tries the known table paths (ADOM and global), prints the count and a few names, and the FortiGuard / ISDB
status. Asks the password itself.
Run: .venv/bin/python tests/probe_fmg_isdb.py <fmg-ip> <adom> [admin]"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError  # noqa: E402

args = sys.argv[1:]
host, adom = args[0], args[1]
user = args[2] if len(args) > 2 else "admin"
pw = getpass.getpass(f"wachtwoord voor {user}@{host}: ").strip("\r\n")   # pct exec has no tty: a CR can stick to it
fmg = Fmg(host, user, pw)
fmg.login()

PATHS = [f"/pm/config/adom/{adom}/obj/firewall/internet-service-name",
         f"/pm/config/adom/{adom}/obj/firewall/internet-service",
         "/pm/config/global/obj/firewall/internet-service-name",
         "/pm/config/global/obj/firewall/internet-service"]
WORDS = ("office365", "teams", "zoom", "netflix", "youtube", "salesforce")
VARIANTS = [("standaard (fields=name)", {"fields": ["name"]}),
            ("zonder fields", {}),
            ("option get reserved", {"option": ["get reserved"]}),
            ("option get reserved + fields", {"option": ["get reserved"], "fields": ["name"]}),
            ("range 0-20", {"range": [0, 20]}),
            ("loadsub 0 + get reserved", {"loadsub": 0, "option": ["get reserved"]})]
try:
    print("versie:", fmg.call("get", "/sys/status").get("data", {}).get("Version"))
    for url in PATHS:
        print(f"\n{url}")
        for label, extra in VARIANTS:
            try:
                res = fmg.call("get", url, **extra)
            except FmgError as e:
                print(f"  {label:32s} fout: {str(e)[:120]}")
                continue
            rows = res.get("data") or []
            rows = rows if isinstance(rows, list) else [rows]
            names = [r.get("name") for r in rows if isinstance(r, dict) and r.get("name")]
            hit = [n for n in names if any(w in n.lower().replace("-", "").replace(".", "") for w in WORDS)][:4]
            print(f"  {label:32s} aantal {len(names):5d}  eerste {names[:3]}  treffers {hit}")
    for url in ("/cli/global/fmupdate/service", "/cli/global/fmupdate/fds-setting"):
        try:
            print(f"\n{url}:", fmg.call("get", url).get("data"))
        except FmgError as e:
            print(f"\n{url}: fout: {e}")
finally:
    fmg.logout()
