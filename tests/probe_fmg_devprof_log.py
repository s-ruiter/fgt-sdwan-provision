"""Read-only probe of a real FortiManager: the log settings of the system template (devprof) as the GUI saved them
("Send logs to FortiAnalyzer/FortiManager", "Send to: This FortiManager", "Upload option: Real-time"), plus the
syntax (allowed values) of those objects. Asks the password itself; changes nothing.
Run: .venv/bin/python tests/probe_fmg_devprof_log.py <fmg-ip> <adom> [admin] [template]   (template default Basic-Settings)"""
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
    for url in (f"{base}/device/profile/setting", f"{base}/device/profile/fortianalyzer",
                f"{base}/log/fortianalyzer/setting", f"{base}/log/fortianalyzer-cloud/setting", f"{base}/log/syslogd/setting"):
        print(f"\n== {url}")
        try:
            print(json.dumps(fmg.call("get", url).get("data"), indent=1)[:2500])
        except FmgError as e:
            print("  niet leesbaar:", e)
            continue
        try:
            syn = fmg.call("get", url, option="syntax").get("data", {})
            attrs = next(iter(syn.values()), {}).get("attr", {}) if isinstance(syn, dict) else {}
            for k, a in attrs.items():
                if any(w in k for w in ("status", "server", "upload", "target", "managed", "fmg", "faz", "enc", "reliable")):
                    print(f"  {k}: type={a.get('type')} opts={a.get('opts')}")
        except FmgError as e:
            print("  syntax niet leesbaar:", e)
finally:
    fmg.logout()
