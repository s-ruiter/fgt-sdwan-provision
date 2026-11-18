"""Probe a real FortiManager: how do metadata variables keep their per-device values?
Reads every variable in an ADOM (with its per-device mappings) and, with --try, writes the value of ONE variable for ONE
device in two forms (_scope vdom 'root' and 'global') and reads it back after each. Asks the password itself.
Run: .venv/bin/python tests/probe_fmg_vars.py <fmg-ip> <adom> [admin] [--try <variable> <device>]"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError  # noqa: E402

args = sys.argv[1:]
trial = None
if "--try" in args:
    i = args.index("--try")
    trial, args = args[i + 1:i + 3], args[:i]
host, adom = args[0], args[1]
user = args[2] if len(args) > 2 else "admin"
fmg = Fmg(host, user, getpass.getpass(f"wachtwoord voor {user}@{host}: "))
fmg.login()
base = f"/pm/config/adom/{adom}/obj/fmg/variable"


def show(name=None):
    url = f"{base}/{name}" if name else base
    data = fmg.call("get", url).get("data")
    rows = data if isinstance(data, list) else [data]
    for v in rows:
        dm = v.get("dynamic_mapping") or []
        print(f"  {v.get('name'):24s} default={v.get('value')!r:12s} mappings={len(dm)}",
              json.dumps([{"scope": m.get("_scope"), "value": m.get("value")} for m in dm])[:200])


try:
    print("versie:", fmg.call("get", "/sys/status").get("data", {}).get("Version"))
    print(f"\nvariabelen in ADOM {adom}:")
    show()
    try:
        dm = fmg.call("get", f"{base}/loopback/dynamic_mapping").get("data")
        print("\nloopback/dynamic_mapping (apart opgevraagd):", json.dumps(dm)[:400])
    except FmgError as e:
        print("\nloopback/dynamic_mapping niet leesbaar:", e)
    if trial:
        var, dev = trial
        for vdom in ("root", "global"):
            try:
                r = fmg.call("set", f"{base}/{var}", {"name": var, "dynamic_mapping": [
                    {"_scope": [{"name": dev, "vdom": vdom}], "value": f"probe-{vdom}"}]})
                print(f"\nset met vdom={vdom}: {r.get('status')}")
            except FmgError as e:
                print(f"\nset met vdom={vdom} geweigerd: {e}")
            show(var)
        try:
            r = fmg.call("add", f"{base}/{var}/dynamic_mapping", [{"_scope": [{"name": dev, "vdom": "global"}], "value": "probe-sub"}])
            print(f"\nadd op .../{var}/dynamic_mapping (vdom global): {r.get('status')}")
        except FmgError as e:
            print(f"\nadd op .../{var}/dynamic_mapping geweigerd: {e}")
        show(var)
        print("\n(de tool zet de echte waarden terug bij de volgende uitrol)")
finally:
    fmg.logout()
