"""Read-only probe of a real FortiManager: current values + allowed options for the system settings the tool sets
(idle timeout, ADOM mode, FortiAnalyzer features, timezone). Asks the password itself; changes nothing,
unless --apply is given: then it runs the tool's own apply_fmg_settings (FortiAnalyzer features -> FMG reboots).
Run: .venv/bin/python tests/probe_fmg_settings.py <fmg-ip> [admin] [--apply]"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError, apply_fmg_settings  # noqa: E402

APPLY = "--apply" in sys.argv
args = [a for a in sys.argv[1:] if a != "--apply"]
host = args[0]
user = args[1] if len(args) > 1 else "admin"
fmg = Fmg(host, user, getpass.getpass(f"wachtwoord voor {user}@{host}: "))
fmg.login()
if APPLY:
    apply_fmg_settings(fmg, {"login_delay": 15}, lambda s, st, m: print(f"[{st}] {m}", flush=True))
try:
    print("versie:", fmg.call("get", "/sys/status").get("data", {}).get("Version"))
    for url, keys in (("/cli/global/system/global", ("adom-status", "adom-mode", "faz-status", "timezone")),
                      ("/cli/global/system/admin/setting", None)):
        data = fmg.call("get", url).get("data", {})
        shown = {k: v for k, v in data.items() if keys is None and "time" in k or keys and k in keys}
        print(f"\n{url} nu:", json.dumps(shown, indent=1))
        try:
            syn = fmg.call("get", url, option="syntax").get("data", {})
            attrs = next(iter(syn.values()), {}).get("attr", {}) if isinstance(syn, dict) else {}
            for k, a in attrs.items():
                if (keys and k in keys) or (keys is None and "time" in k):
                    opts = a.get("opts")
                    if k == "timezone" and isinstance(opts, dict):
                        opts = {n: v for n, v in opts.items() if "msterdam" in n or "erlin" in n or str(v) in ("26", "28")} or list(opts)[:10]
                    print(f"  {k}: type={a.get('type')} opts={opts} min={a.get('min')} max={a.get('max')}")
        except FmgError as e:
            print("  syntax niet leesbaar:", e)
finally:
    fmg.logout()
