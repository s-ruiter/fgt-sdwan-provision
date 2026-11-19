"""FortiFlex API v2: one license (entitlement = serial number + token) per device, reused across deployments.

Login: POST https://customerapiauth.fortinet.com/api/v1/oauth/token/ (client_id flexvm, grant_type password) gives a
Bearer token for https://support.fortinet.com/ES/api/fortiflex/v2. Every call is a POST with JSON; status 0 = ok.
Limits: 100 calls/minute, 3000/hour."""
import requests

AUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
BASE = "https://support.fortinet.com/ES/api/fortiflex/v2"
PREFIX = "fgt-sdwan"
PLACEHOLDER = {"fgt": "FGVMNEWLIC{:06d}", "fmg": "FMGVMNEWLC{:06d}"}   # dry-run / check: no license yet (unique)
USABLE = ("ACTIVE", "PENDING")


class FlexError(Exception):
    pass


def desc(adom, name):
    return f"{PREFIX} {adom} {name}"


class Client:
    def __init__(self, user, password, auth_url=None, base=None, timeout=30):
        self.user, self.password, self.timeout = user, password, timeout
        self.auth_url, self.base = auth_url or AUTH_URL, base or BASE        # module values: tests point them elsewhere
        self._token = None

    def _login(self):
        try:
            r = requests.post(self.auth_url, json={"username": self.user, "password": self.password,
                                                   "client_id": "flexvm", "grant_type": "password"}, timeout=self.timeout)
        except requests.RequestException as e:
            raise FlexError(f"FortiFlex: inloggen mislukt, geen verbinding met {self.auth_url}: {type(e).__name__}") from None
        if r.status_code != 200 or "access_token" not in (r.json() if r.content else {}):
            try:
                why = r.json().get("error_description") or r.json().get("error") or ""
            except ValueError:
                why = ""
            raise FlexError(f"FortiFlex: inloggen geweigerd (HTTP {r.status_code}) {why}".strip()
                            + " - controleer API-gebruiker en wachtwoord op de pagina FortiFlex")
        self._token = r.json()["access_token"]

    def call(self, path, body=None):
        if not self._token:
            self._login()
        try:
            r = requests.post(self.base + path, json=body or {}, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self._token}"})
        except requests.RequestException as e:
            raise FlexError(f"FortiFlex {path}: geen verbinding ({type(e).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or data.get("status") != 0:
            msg = data.get("error") or data.get("message") or f"HTTP {r.status_code}"
            if r.status_code in (401, 403):
                msg += " (heeft de API-gebruiker rechten op FortiFlex? Aanmaken vraagt ReadWrite)"
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

    def create_vm(self, config_id, description):
        return self._one("/entitlements/vm/create", {"configId": int(config_id), "count": 1, "description": description,
                                                     "endDate": None})

    def regen_token(self, sn):
        return self._one("/entitlements/vm/token", {"serialNumber": sn})

    def reactivate(self, sn):
        return self._one("/entitlements/reactivate", {"serialNumber": sn, "followProgram": True})

    def update(self, sn, config_id, description):
        return self._one("/entitlements/update", {"serialNumber": sn, "configId": int(config_id), "description": description})

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


ACTION_TEXT = {"use": "bestaande licentie, token ongebruikt", "regen": "bestaande licentie, nieuw token",
               "reactivate": "gestopte licentie, opnieuw activeren + nieuw token", "create": "nieuwe licentie (kost punten)"}


def resolve(client, rows, dry):
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
                e = client.create_vm(r["config_id"], r["description"])
                sn, tok = e["serialNumber"], e.get("token")
                created.append(f"{r['name']} ({sn}) aangemaakt")
            else:
                if r.get("rename"):
                    client.update(sn, r["config_id"], r["description"])
                if act == "reactivate":
                    client.reactivate(sn)
                    created.append(f"{r['name']} ({sn}) heractiveerd")
                if act in ("reactivate", "regen"):
                    tok = client.regen_token(sn).get("token")
                    created.append(f"{r['name']} ({sn}) nieuw token")
        except FlexError as e:
            extra = f" (in deze uitrol al gedaan: {'; '.join(created)})" if created else ""
            raise FlexError(f"{r['name']}: {e}{extra}") from None
        if not tok:
            raise FlexError(f"{r['name']}: FortiFlex gaf geen token voor {sn}")
        out[r["name"]] = {"serial": sn, "token": tok, "action": act}
    return out
