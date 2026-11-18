"""Read-only probe of a real FortiManager: why does auto-link stop at 'devmgmtdatafailed | Run script fail'?
Compares a failing device with a working one: script logs FortiManager kept for each, and in the device database
port1, the static routes and central-management. Asks the password itself; changes nothing.
Run: .venv/bin/python tests/probe_fmg_mgmtdata.py <fmg-ip> <adom> <failing-dev> <working-dev> [admin]"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import Fmg, FmgError  # noqa: E402

host, adom, bad, good = sys.argv[1:5]
user = sys.argv[5] if len(sys.argv) > 5 else "admin"
fmg = Fmg(host, user, getpass.getpass(f"wachtwoord voor {user}@{host}: "))
fmg.login()


def get(url, **kw):
    try:
        return fmg.call("get", url, **kw).get("data")
    except FmgError as e:
        return f"<{e}>"


try:
    for dev in (bad, good):
        print(f"\n=================== {dev}")
        print("script-logs:", json.dumps(get(f"/dvmdb/adom/{adom}/script/log/list/device/{dev}"))[:1500])
        latest = get(f"/dvmdb/adom/{adom}/script/log/latest/device/{dev}")
        print("laatste script-log:", json.dumps(latest)[:3000])
        base = f"/pm/config/device/{dev}/global/system"
        p1 = get(f"{base}/interface/port1")
        if isinstance(p1, dict):
            p1 = {k: p1.get(k) for k in ("name", "mode", "ip", "vrf", "allowaccess", "role", "type", "vdom")}
        print("device-DB port1:", json.dumps(p1))
        print("device-DB central-management:", json.dumps(get(f"{base}/central-management"))[:800])
        routes = get(f"/pm/config/device/{dev}/vdom/root/router/static")
        print("device-DB router static:", json.dumps(routes)[:1500])
        d = get(f"/dvmdb/adom/{adom}/device/{dev}")
        if isinstance(d, dict):
            print("dvmdb:", json.dumps({k: d.get(k) for k in ("ip", "mgmt_mode", "conn_status", "conf_status", "db_status",
                                                              "flags", "mgmt_if", "mgmt_uuid", "faz.perm")}))
finally:
    fmg.logout()
