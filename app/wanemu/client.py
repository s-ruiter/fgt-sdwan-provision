"""The tool's side of the WAN-emulator agent: HTTP client and presets. The agent token never leaves the tool."""
import requests

# values per link, applied in both directions (so a delay counts twice in the round trip)
PRESETS = {
    "neutraal": {"label": "Neutraal", "values": {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0, "rate_kbit": 0, "down": False}},
    "glasvezel": {"label": "Glasvezel", "values": {"delay_ms": 2, "jitter_ms": 0, "loss_pct": 0, "rate_kbit": 1000000, "down": False}},
    "dsl": {"label": "DSL", "values": {"delay_ms": 15, "jitter_ms": 3, "loss_pct": 0.1, "rate_kbit": 50000, "down": False}},
    "slechte-4g": {"label": "Slechte 4G", "values": {"delay_ms": 60, "jitter_ms": 25, "loss_pct": 3, "rate_kbit": 10000, "down": False}},
    "satelliet": {"label": "Satelliet", "values": {"delay_ms": 300, "jitter_ms": 20, "loss_pct": 0.5, "rate_kbit": 20000, "down": False}},
    "trans-atlantisch": {"label": "Trans-Atlantisch", "values": {"delay_ms": 45, "jitter_ms": 2, "loss_pct": 0, "rate_kbit": 0, "down": False}},
    "isp-a-storing": {"label": "ISP-A storing", "seg": "a", "values": {"down": True}},
    "isp-b-storing": {"label": "ISP-B storing", "seg": "b", "values": {"down": True}},
    "hub1-weg": {"label": "Hub 1 weg", "hub": 0, "values": {"down": True}},
}


class AgentError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def preset_changes(name, links, link=None):
    """[(link_id, values)] for preset `name`; `link` limits a value preset to one link. KeyError if unknown."""
    p = PRESETS[name]
    if "seg" in p:
        targets = [l for l in links if l["seg"] == p["seg"]]
    elif "hub" in p:
        targets = [l for l in links if l["seg"] == "hub"][p["hub"]:p["hub"] + 1]
    else:
        targets = [l for l in links if link in (None, l["id"])]
    return [(l["id"], dict(p["values"])) for l in targets]


class Agent:
    def __init__(self, addr, token, timeout=5):
        self.addr, self.base, self.token, self.timeout = addr, f"http://{addr}", token, timeout

    def _req(self, method, path, body=None):
        try:
            r = requests.request(method, self.base + path, json=body, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {self.token}"})
        except requests.RequestException:
            raise AgentError(f"WAN-emulator niet bereikbaar op {self.addr} (draait de container wan-emu?)") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 401:
            raise AgentError("WAN-emulator weigert het token: rol de FortiGates opnieuw uit om de agent bij te werken")
        if r.status_code >= 400:
            raise AgentError(data.get("error") or f"WAN-emulator gaf HTTP {r.status_code}", r.status_code)
        return data

    def links(self):
        return self._req("GET", "/links")["links"]

    def put(self, link_id, values):
        return self._req("PUT", f"/links/{link_id}", values)

    def reset(self):
        return self._req("POST", "/reset")["links"]
