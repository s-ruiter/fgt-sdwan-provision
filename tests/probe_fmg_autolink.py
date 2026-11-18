"""Read-only probe of a real FortiManager: why does auto-link of model devices fail?
Shows the model devices of an ADOM (platform, serial, status), unregistered devices that announced themselves, and the
lines of recent tasks that mention auto-link or the devices. Asks the password itself; changes nothing.
Run: .venv/bin/python tests/probe_fmg_autolink.py <fmg-ip> <adom> [admin]"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError  # noqa: E402

host, adom = sys.argv[1], sys.argv[2]
user = sys.argv[3] if len(sys.argv) > 3 else "admin"
fmg = Fmg(host, user, getpass.getpass(f"wachtwoord voor {user}@{host}: "))
fmg.login()
KEYS = ("name", "sn", "platform_str", "os_ver", "mr", "build", "mgmt_mode", "conn_status", "conf_status", "dev_status",
        "ip", "hostname", "flags")
try:
    devs = fmg.call("get", f"/dvmdb/adom/{adom}/device").get("data") or []
    names = {d.get("name") for d in devs}
    print(f"model devices in {adom}:")
    for d in devs:
        print("  ", json.dumps({k: d.get(k) for k in KEYS}))
    try:
        unreg = fmg.call("get", "/dvmdb/device", filter=["mgmt_mode", "==", "unreg"]).get("data") or []
    except FmgError as e:
        unreg, _ = [], print("unregistered niet leesbaar:", e)
    print("\nniet-geregistreerde devices (aangemeld, niet gekoppeld):")
    for d in unreg:
        print("  ", json.dumps({k: d.get(k) for k in KEYS}))
    tasks = fmg.call("get", "/task/task").get("data") or []
    tasks = sorted(tasks, key=lambda t: t.get("id", 0), reverse=True)[:40]
    print("\nrecente taken over auto-link / deze devices:")
    for t in tasks:
        title = str(t.get("title", ""))
        if "link" in title.lower() or any(n and n in title for n in names) or "auto" in title.lower():
            print(f"  task {t.get('id')}: {title} state={t.get('state')} err={t.get('num_err')}")
            try:
                for ln in fmg.call("get", f"/task/task/{t['id']}/line").get("data") or []:
                    print(f"      {ln.get('name')}: {ln.get('state')} {str(ln.get('detail'))[:300]}")
                    for h in ln.get("history") or []:
                        print(f"         - {str(h.get('detail'))[:400]}")
                    if not ln.get("history") and ln.get("oid") is not None:
                        try:
                            for h in fmg.call("get", f"/task/task/{t['id']}/line/{ln['oid']}/history").get("data") or []:
                                print(f"         - {str(h.get('detail'))[:400]}")
                        except FmgError as e:
                            print("         (geen history:", str(e)[:120], ")")
            except FmgError as e:
                print("      regels niet leesbaar:", e)
finally:
    fmg.logout()
