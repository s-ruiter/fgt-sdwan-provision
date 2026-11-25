"""Turns a plan (app/fmgtemplates.py) into the ordered FortiManager JSON-RPC calls and runs them.

All calls are idempotent upserts (`set`/`replace`/`update`) on objects that belong to the ADOM this tool created.
Paths follow the user's Postman collection (s-ruiter/sdwan-fmg-provision); they are NOT verified against a real
FortiManager 8.0 yet.
"""
import copy
import re

from .fmg import FmgError

PKG_SETTINGS = {"central-nat": 0, "consolidated-firewall-mode": 0, "fwpolicy-implicit-log": 0,
                "fwpolicy6-implicit-log": 0, "ngfw-mode": 0}
BLUEPRINT_PLATFORM = "FortiGate-VM64-KVM"
# FortiManager's predefined metadata variable for the number of FortiGate-VM interfaces (factory default 1);
# every FortiGate-VM in this design has 6 interfaces (user decision), see also provision_vms.MIN_FGT_NICS.
VM_INTERFACE_NUMBER = 6


def plan_calls(topo, plan, devices=True):
    """-> list of dicts(stage, method, url, data, ignore_exists)

    devices=False ('Alleen FortiManager'): there are no (model) devices yet, so nothing refers to a device: device
    groups stay empty, metadata variables and IP pools get no per-device mapping. A later run with devices fills them."""
    adom = topo["adom"]
    base = f"/pm/config/adom/{adom}"
    calls = []

    def add(stage, method, url, data, ignore=False, soft=False):
        calls.append({"stage": stage, "method": method, "url": url, "data": data, "ignore": ignore, "soft": soft})

    roles = plan["roles"]
    group = lambda r: r["name"].lower()
    scope = lambda names: [{"name": n, "vdom": "root"} for n in names]
    # metadata variables are DEVICE-level: FortiManager shows/uses the per-device value only for vdom "global"
    # (a "root" mapping is stored and returned by the API, but the device's metadata view stays empty; FMG 8.0.2)
    dev_scope = lambda names: [{"name": n, "vdom": "global"} for n in names]

    # 1. device groups (+ members)
    for r in roles:
        add("Device groups", "set", f"/dvmdb/adom/{adom}/group", [{"name": group(r), "os_type": "fos"}])
        if devices:
            add("Device groups", "add", f"/dvmdb/adom/{adom}/group/{group(r)}/object member", scope(r["devices"]), ignore=True)

    # 2. metadata variables with the value per device
    for v, vals in sorted(plan["variables"].items()):
        body = {"name": v}
        if devices:
            body["dynamic_mapping"] = [{"_scope": dev_scope([d]), "value": str(val)} for d, val in sorted(vals.items())]
        add("Metadata variables", "set", f"{base}/obj/fmg/variable/{v}", body)
    # update (not set): keep FortiManager's own description of this predefined variable; soft: older FMGs lack it
    add("Metadata variables", "update", f"{base}/obj/fmg/variable/vm_interface_number",
        {"name": "vm_interface_number", "value": str(VM_INTERFACE_NUMBER)}, soft=True)

    # 3. system template (DNS, admin)
    dns1, dns2 = plan["dns"]
    add("System template", "set", f"/pm/devprof/adom/{adom}/", [{"name": "Basic-Settings", "type": "devprof"}])
    add("System template", "set", f"{base}/devprof/Basic-Settings/device/profile/setting", {"enabled-pages": ["dns", "log", "admin"]})
    add("System template", "set", f"{base}/devprof/Basic-Settings/system/global", {"admintimeout": 480, "admin-scp": 1})
    dns = {"primary": dns1, "protocol": ["cleartext"]}     # FortiOS 8.0 default = DoT to globalsdns.fortinet.net
    if dns2:
        dns["secondary"] = dns2
    add("System template", "set", f"{base}/devprof/Basic-Settings/system/dns", dns)

    # 4. SD-WAN templates (native) + static route templates
    add("SD-WAN templates", "set", f"/pm/wanprof/adom/{adom}/", [{"name": r["name"], "type": "wanprof"} for r in roles])
    for r in roles:
        body = copy.deepcopy(r["sdwan"])
        advpn = {}
        for z in body.get("zone", []):
            for k in ("advpn-select", "advpn-health-check"):
                if k in z:
                    advpn.setdefault(z["name"], {})[k] = z.pop(k)
        if not any(z["name"] == "virtual-wan-link" for z in body.get("zone", [])):
            body.setdefault("zone", []).insert(0, {"name": "virtual-wan-link"})   # reserved zone: FMG refuses to delete it
        add("SD-WAN templates", "update", f"{base}/wanprof/{r['name']}/system/sdwan", body)
        for zname, vals in advpn.items():
            add("SD-WAN templates", "update", f"{base}/wanprof/{r['name']}/system/sdwan/zone/{zname}", vals)
    for r in roles:
        add("Static-route templates", "set", f"/pm/template/_router_static/adom/{adom}/",
            [{"name": f"Underlay-{r['name']}", "type": "template", "template setting": {"stype": "_router_static", "widgets": ["_router_static"]}}])
        add("Static-route templates", "set", f"{base}/template/_router_static/Underlay-{r['name']}/router/static/", r["static"])

    # 5. interfaces, addresses, pools
    ifs = sorted({i for r in roles for i in r["dyn_ifaces"]})
    for i in ifs:
        add("Interfaces and addresses", "set", f"{base}/obj/dynamic/interface/{i}",
            {"name": i, "description": i, "single-intf": 1, "default-mapping": 1, "defmap-intf": i, "color": 2})
    seen = set()
    for r in roles:
        for a in r["addresses"]:
            if a["name"] not in seen:
                seen.add(a["name"])
                add("Interfaces and addresses", "set", f"{base}/obj/firewall/address/{a['name']}", a)
    pools = {}
    for r in roles:
        for dn, rows in r["ippool_map"].items():
            for name, vals in rows:
                pools.setdefault(name, []).append((dn, vals))
    for name, lst in pools.items():
        body = {"name": name, "type": "overload", "startip": lst[0][1]["startip"], "endip": lst[0][1]["endip"]}
        if devices:
            body["dynamic_mapping"] = [{"_scope": scope([dn]), "startip": v["startip"], "endip": v["endip"]} for dn, v in lst]
        add("Interfaces and addresses", "set", f"{base}/obj/firewall/ippool/{name}", body)

    # 6. policy packages
    add("Policy packages", "set", f"/pm/pkg/adom/{adom}",
        [{"name": r["name"], "package settings": PKG_SETTINGS, "scope member": [{"name": group(r)}], "type": "pkg"} for r in roles])
    for r in roles:
        add("Policy packages", "set", f"{base}/pkg/{r['name']}/firewall/policy/", r["policies"])

    # 7. CLI templates + CLI template groups
    for r in roles:
        for name, text, vs in r["cli"]:
            add("CLI templates", "set", f"{base}/obj/cli/template/{name}",
                {"name": name, "description": f"SD-WAN multi-VRF {r['name']} (fgt-sdwan-provision)", "position": "post-vdom-copy",
                 "provision": "disable", "script": text + "\n", "type": "cli", "variables": vs})
        add("CLI templates", "set", f"{base}/obj/cli/template-group/{r['name']}",
            {"name": r["name"], "member": [n for n, _, _ in r["cli"]]})

    # 8. provisioning template groups, scoped to the device group
    for r in roles:
        add("Template groups", "set", f"/pm/tmplgrp/adom/{adom}",
            {"name": r["name"], "type": "tmplgrp", "scope member": [],
             "template group setting": {"description": "", "cliprofs": [r["name"]], "wtpprofs": [], "fspprofs": [], "fxtprofs": [],
                                        "templates": ["1__Basic-Settings", f"4-2__Underlay-{r['name']}", f"5__{r['name']}"]}})
        add("Template groups", "update", f"/pm/tmplgrp/adom/{adom}",
            [{"name": r["name"], "type": "tmplgrp", "scope member": [{"name": group(r), "is group": 1}]}])

    # 9. blueprints
    for r in roles:
        add("Blueprints", "set", f"{base}/obj/fmg/device/blueprint/{r['name']}",
            {"name": r["name"], "dev-group": group(r), "prov-type": "template-group", "template-group": r["name"],
             "pkg": r["name"], "platform": BLUEPRINT_PLATFORM, "vm-log-disk": 1})
    order = ["Device groups", "Metadata variables", "System template", "Interfaces and addresses", "SD-WAN templates",
             "Static-route templates", "Policy packages", "CLI templates", "Template groups", "Blueprints"]
    return sorted(calls, key=lambda c: order.index(c["stage"]))      # stable: keeps order within a stage


ISDB_PER_KEYWORD = 10


def _norm(text):
    return " " + re.sub(r"[-._/\s]+", " ", text.lower()).strip() + " "


def resolve_isdb(names, plan, why=""):
    """Turn the demo rules' keywords into internet-service names the FortiManager has (word match, at most 10 per
    keyword); a rule without any hit is dropped. names=[] (list not available): all demo rules dropped. Returns
    warnings. Removes every '_isdb_keywords'."""
    norm = [(n, _norm(n)) for n in sorted(set(names or []))]
    warns, skipped_all = [], False
    for r in plan["roles"]:
        sv = r["sdwan"].get("service", [])
        keep = []
        for row in sv:
            kws = row.pop("_isdb_keywords", None)
            if kws is None:
                keep.append(row)
                continue
            if not norm:
                skipped_all = True
                continue
            hit = []
            for kw in kws:
                k = _norm(kw)
                hit += [n for n, nn in norm if k in nn and n not in hit][:ISDB_PER_KEYWORD]
            if not hit:
                w = f"{row['name']}: no internet services found for {', '.join(kws)} (demo SD-WAN rule skipped)"
                if w not in warns:
                    warns.append(w)
                continue
            keep.append(dict(row, **{"internet-service": "enable", "internet-service-name": sorted(hit)}))
        if sv:
            r["sdwan"]["service"] = keep
    if skipped_all:
        warns.insert(0, "demo SD-WAN rules skipped: the FortiManager's internet-service list is not available"
                     + (f" ({why})" if why else " (empty: FortiGuard ISDB not downloaded?)"))
    return warns


def apply_plan(fmg, topo, plan, emit, step="templates", devices=True):
    why = ""
    if fmg.dry:
        names = None
    else:
        try:
            # the ISDB entries are built-in (reserved) objects: FMG 8.0.2 returns them only with option "get reserved"
            # (without it: 0 of 1773 entries)
            rows = fmg.call("get", f"/pm/config/adom/{topo['adom']}/obj/firewall/internet-service-name",
                            fields=["name"], option=["get reserved"]).get("data") or []
            names = [x["name"] for x in rows if isinstance(x, dict) and x.get("name")]
        except FmgError as e:
            names, why = [], f"not readable: {e}"
    if names is None:                                    # dry run: nothing is looked up, the demo rules are left out
        for r in plan["roles"]:
            r["sdwan"]["service"] = [x for x in r["sdwan"].get("service", []) if "_isdb_keywords" not in x]
        emit(step, "running", "dry run: the demo SD-WAN rules are matched with the FortiManager at deployment")
    else:
        for w in resolve_isdb(names, plan, why):
            emit(step, "warn", w)
        used = sorted({n for r in plan["roles"] for x in r["sdwan"].get("service", [])
                       for n in x.get("internet-service-name", [])})
        if used:
            emit(step, "running", f"demo SD-WAN rules: internet services {', '.join(used)}")
    calls = plan_calls(topo, plan, devices)
    stages = []
    for c in calls:
        if c["stage"] not in stages:
            stages.append(c["stage"])
    done = 0
    for st in stages:
        n = 0
        for c in (x for x in calls if x["stage"] == st):
            try:
                fmg.call(c["method"], c["url"], c["data"])
            except FmgError as e:
                low = str(e).lower()
                if c["ignore"] and ("exist" in low or "duplicate" in low):
                    pass
                elif c.get("soft"):
                    emit(step, "warn", f"{st}: {e}")
                else:
                    raise FmgError(f"{st}: {e}")
            n += 1
        done += n
        emit(step, "running", f"{st}: {n} call(s){' [dry-run]' if fmg.dry else ''}")
    for w in plan["warnings"]:
        emit(step, "warn", w)
    return done
