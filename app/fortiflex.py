"""FortiFlex API v2: one license (entitlement = serial number + token) per device, reused across deployments.

Login: POST https://customerapiauth.fortinet.com/api/v1/oauth/token/ (client_id flexvm, grant_type password) gives a
Bearer token for https://support.fortinet.com/ES/api/fortiflex/v2. Every call is a POST with JSON; status 0 = ok.
Limits: 100 calls/minute, 3000/hour."""
import requests

AUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
BASE = "https://support.fortinet.com/ES/api/fortiflex/v2"
ASSET_BASE = "https://support.fortinet.com/ES/api/registration/v3"   # Asset Management API v3 (folders)
PREFIX = "fgt-sdwan"
PLACEHOLDER = {"fgt": "FGVMNEWLIC{:06d}", "fmg": "FMGVMNEWLC{:06d}"}   # dry-run / check: no license yet (unique)
USABLE = ("ACTIVE", "PENDING")


class FlexError(Exception):
    pass


def check_folder(path):
    """Asset folder for new licenses: empty (FortiFlex default) or a path under 'My Assets'. Returns it normalised."""
    p = (path or "").strip().strip("/").strip()
    if not p:
        return ""
    parts = p.split("/")
    if parts[0] != "My Assets" or any(not x.strip() for x in parts) or len(p) > 255:
        raise ValueError("Asset folder must be a path under 'My Assets', e.g. My Assets/Lab/SD-WAN (or empty)")
    return "/".join(x.strip() for x in parts)


def desc(adom, name):
    return f"{PREFIX} {adom} {name}"


class Client:
    def __init__(self, user, password, auth_url=None, base=None, timeout=30):
        self.user, self.password, self.timeout = user, password, timeout
        self.auth_url, self.base = auth_url or AUTH_URL, base or BASE        # module values: tests point them elsewhere
        self._tokens = {}                                  # per OAuth client: flexvm (FortiFlex), assetmanagement

    def _login(self, client_id="flexvm"):
        try:
            r = requests.post(self.auth_url, json={"username": self.user, "password": self.password,
                                                   "client_id": client_id, "grant_type": "password"}, timeout=self.timeout)
        except requests.RequestException as e:
            raise FlexError(f"FortiFlex: login failed, no connection to {self.auth_url}: {type(e).__name__}") from None
        if r.status_code != 200 or "access_token" not in (r.json() if r.content else {}):
            try:
                why = r.json().get("error_description") or r.json().get("error") or ""
            except ValueError:
                why = ""
            what = "FortiFlex" if client_id == "flexvm" else "Asset Management"
            raise FlexError(f"{what}: login refused (HTTP {r.status_code}) {why}".strip()
                            + " - check the API user, the password and the API user's permissions")
        self._tokens[client_id] = r.json()["access_token"]

    def call(self, path, body=None, base=None, client_id="flexvm"):
        if client_id not in self._tokens:
            self._login(client_id)
        try:
            r = requests.post((base or self.base) + path, json=body or {}, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self._tokens[client_id]}"})
        except requests.RequestException as e:
            raise FlexError(f"FortiFlex {path}: no connection ({type(e).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or data.get("status") != 0:
            msg = data.get("error") or data.get("message") or f"HTTP {r.status_code}"
            if r.status_code in (401, 403):
                msg += " (does the API user have FortiFlex permissions? Creating needs ReadWrite)"
            raise FlexError(f"FortiFlex {path}: {msg}")
        return data

    def programs(self):
        return self.call("/programs/list")["programs"]

    def configs(self, program_sn):
        return self.call("/configs/list", {"programSerialNumber": program_sn})["configs"]

    def entitlements(self, config_id):
        return self.call("/entitlements/list", {"configId": int(config_id)})["entitlements"]

    def _one(self, path, body):
        return self.call(path, body)["entitlements"][0]

    def create_vm(self, config_id, description, folder=""):
        body = {"configId": int(config_id), "count": 1, "description": description, "endDate": None}
        if folder:
            body["folderPath"] = folder                         # Asset Management folder, e.g. "My Assets/Lab"
        return self._one("/entitlements/vm/create", body)

    def regen_token(self, sn):
        return self._one("/entitlements/vm/token", {"serialNumber": sn})

    def reactivate(self, sn):
        return self._one("/entitlements/reactivate", {"serialNumber": sn, "followProgram": True})

    def update(self, sn, config_id, description):
        return self._one("/entitlements/update", {"serialNumber": sn, "configId": int(config_id), "description": description})

    def asset_folders(self, account_id=None):
        """Asset Management folders as FortiFlex folderPath values ('My Assets/<path>'), sorted."""
        body = {"accountId": int(account_id)} if account_id else {}
        rows = self.call("/folders/list", body, base=ASSET_BASE, client_id="assetmanagement").get("assetFolders") or []
        out = []
        for f in rows:
            p = str(f.get("folderPath") or "").strip("/")
            if p and not p.startswith("My Assets"):
                p = "My Assets/" + p
            if p:
                out.append({"id": f.get("folderId"), "path": p})
        return sorted(out, key=lambda x: x["path"].lower())

    def stop(self, sn):
        return self._one("/entitlements/stop", {"serialNumber": sn})


def plan(devices, ents_by_config, adom):
    """devices: [{name, serial, config_id, role ('fgt'|'fmg', optional)}]. Per device: the entitlement to use (first the
    serial already in the topology, then the tool description) and what to do with it. Each entitlement serves one
    device; one in another configuration or EXPIRED is not used."""
    taken, rows = set(), []
    for d in devices:
        cid = int(d["config_id"])
        pool = [e for e in ents_by_config.get(cid, []) if e["serialNumber"] not in taken and e.get("status") != "EXPIRED"]
        want = desc(adom, d["name"])
        e = next((x for x in pool if d.get("serial") and x["serialNumber"] == d["serial"].strip()), None) or \
            next((x for x in pool if x.get("description") == want), None)
        row = {"name": d["name"], "role": d.get("role", "fgt"), "config_id": cid, "description": want,
               "serial": None, "action": "create", "rename": False, "token": None}
        if e:
            taken.add(e["serialNumber"])
            st = e.get("status")
            action = ("reactivate" if st not in USABLE else "use" if e.get("tokenStatus") == "NOTUSED" else "regen")
            row.update(serial=e["serialNumber"], action=action, rename=e.get("description") != want,
                       token=e.get("token") if action == "use" else None)
        rows.append(row)
    return rows


ACTION_TEXT = {"use": "existing licence, token unused", "regen": "existing licence, new token",
               "reactivate": "stopped licence, reactivate + new token", "create": "new licence (costs points)"}


def resolve(client, rows, dry, folder=""):
    """Carry out the plan. dry=True: only report (placeholder serial for new ones, no tokens).
    Returns {name: {serial, token, action}}. A failure names the licenses already created in this run."""
    out, created, n = {}, [], 0
    for r in rows:
        sn, tok, act = r["serial"], r.get("token"), r["action"]
        if dry:
            if not sn:
                n += 1
                sn = PLACEHOLDER.get(r["role"], PLACEHOLDER["fgt"]).format(n)
            out[r["name"]] = {"serial": sn, "token": None, "action": act}
            continue
        try:
            if act == "create":
                e = client.create_vm(r["config_id"], r["description"], folder)
                sn, tok = e["serialNumber"], e.get("token")
                created.append(f"{r['name']} ({sn}) created")
            else:
                if r.get("rename"):
                    client.update(sn, r["config_id"], r["description"])
                if act == "reactivate":
                    client.reactivate(sn)
                    created.append(f"{r['name']} ({sn}) reactivated")
                if act in ("reactivate", "regen"):
                    tok = client.regen_token(sn).get("token")
                    created.append(f"{r['name']} ({sn}) new token")
        except FlexError as e:
            extra = f" (already done in this deployment: {'; '.join(created)})" if created else ""
            raise FlexError(f"{r['name']}: {e}{extra}") from None
        if not tok:
            raise FlexError(f"{r['name']}: FortiFlex returned no token for {sn}")
        out[r["name"]] = {"serial": sn, "token": tok, "action": act}
    return out
