"""Probe a real FortiManager: interfaces of a FortiGate-VM model device in the device database, and (with --try) whether
a physical port can be added through the API. Asks the password itself.
Run: .venv/bin/python tests/probe_fmg_ports.py <fmg-ip> <device> [admin] [--try <port>]"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError  # noqa: E402

args = sys.argv[1:]
port = None
if "--try" in args:
    i = args.index("--try")
    port, args = args[i + 1], args[:i]
host, dev = args[0], args[1]
user = args[2] if len(args) > 2 else "admin"
fmg = Fmg(host, user, getpass.getpass(f"wachtwoord voor {user}@{host}: "))
fmg.login()
url = f"/pm/config/device/{dev}/global/system/interface"


def show():
    data = fmg.call("get", url, fields=["name", "type", "vdom", "mode"]).get("data") or []
    print(f"  {len(data)} interfaces:", ", ".join(f"{i.get('name')}({i.get('type')})" for i in data))


try:
    print("versie:", fmg.call("get", "/sys/status").get("data", {}).get("Version"))
    d = fmg.call("get", f"/dvmdb/device/{dev}").get("data") or {}
    print("device:", {k: d.get(k) for k in ("name", "platform_str", "sn", "mgmt_mode", "os_ver", "mr", "vm_cpu", "vm_mem")})
    print("\nvoor:")
    show()
    if port:
        for method in ("add", "set"):
            try:
                r = fmg.call(method, url, [{"name": port, "vdom": "root", "type": "physical"}])
                print(f"\n{method} {port} (type physical, vdom root): {r.get('status')}")
                break
            except FmgError as e:
                print(f"\n{method} {port} geweigerd: {e}")
        print("na:")
        show()
finally:
    fmg.logout()
