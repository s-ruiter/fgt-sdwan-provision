"""Read-only probe of a real FortiManager: how the system template (devprof) stores the FortiGates' time zone and NTP
(GUI: System Template > Time Zone / NTP), with the allowed values. Asks the password itself; changes nothing.
Set "Time zone: Europe/Amsterdam" and "NTP on" in the GUI on one template first, then run:
  .venv/bin/python tests/probe_fmg_devprof_time.py <fmg-ip> <adom> [admin] [template]   (template default Basic-Settings)"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError  # noqa: E402

host, adom = sys.argv[1], sys.argv[2]
user = sys.argv[3] if len(sys.argv) > 3 else "admin"
tpl = sys.argv[4] if len(sys.argv) > 4 else "Basic-Settings"
fmg = Fmg(host, user, getpass.getpass(f"wachtwoord voor {user}@{host}: "))
fmg.login()
base = f"/pm/config/adom/{adom}/devprof/{tpl}"
try:
    for url, keys in ((f"{base}/device/profile/setting", ("enabled-pages",)),
                      (f"{base}/system/global", ("timezone",)),
                      (f"{base}/system/ntp", None),
                      (f"{base}/system/ntp/ntpserver", None)):
        print(f"\n== {url}")
        try:
            data = fmg.call("get", url).get("data")
            if isinstance(data, dict) and keys:
                data = {k: v for k, v in data.items() if k in keys}
            print(json.dumps(data, indent=1)[:2000])
        except FmgError as e:
            print("  niet leesbaar:", e)
            continue
        try:
            syn = fmg.call("get", url, option="syntax").get("data", {})
            attrs = next(iter(syn.values()), {}).get("attr", {}) if isinstance(syn, dict) else {}
            for k, a in attrs.items():
                if keys is None or k in keys:
                    opts = a.get("opts")
                    if k == "timezone" and isinstance(opts, dict):
                        opts = {n: v for n, v in opts.items() if "msterdam" in str(n) or str(v) in ("26", "28")} or list(opts)[:8]
                    print(f"  {k}: type={a.get('type')} opts={opts}")
        except FmgError as e:
            print("  syntax niet leesbaar:", e)
finally:
    fmg.logout()
