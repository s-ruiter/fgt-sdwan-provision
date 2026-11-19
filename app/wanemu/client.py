"""The tool's side of the WAN-emulator agent: HTTP client and presets. The agent token never leaves the tool."""
import requests

# failure scenarios: several links down at once ("All neutral" / reset clears them)
PRESETS = {
    "isp-a-down": {"label": "ISP-A outage", "seg": "a", "values": {"down": True}},
    "isp-b-down": {"label": "ISP-B outage", "seg": "b", "values": {"down": True}},
    "hub1-down": {"label": "Hub 1 down", "hub": 0, "values": {"down": True}},
}


class AgentError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def preset_changes(name, links, link=None):
    """[(link_id, values)] for failure preset `name` (`link` is accepted and ignored). KeyError if unknown."""
    p = PRESETS[name]
    if "seg" in p:
        targets = [l for l in links if l["seg"] == p["seg"]]
    else:
        targets = [l for l in links if l["seg"] == "hub"][p["hub"]:p["hub"] + 1]
    return [(l["id"], dict(p["values"])) for l in targets]


class Agent:
    def __init__(self, addr, token, timeout=5):
        self.addr, self.base, self.token, self.timeout = addr, f"http://{addr}", token, timeout

    def _req(self, method, path, body=None):
        try:
            r = requests.request(method, self.base + path, json=body, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {self.token}"})
        except requests.RequestException:
            raise AgentError(f"WAN emulator not reachable on {self.addr} (is the wan-emu container running?)") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 401:
            raise AgentError("WAN emulator refuses the token: deploy the FortiGates again to update the agent")
        if r.status_code >= 400:
            raise AgentError(data.get("error") or f"WAN emulator returned HTTP {r.status_code}", r.status_code)
        return data

    def links(self):
        return self._req("GET", "/links")["links"]

    def put(self, link_id, values):
        return self._req("PUT", f"/links/{link_id}", values)

    def reset(self):
        return self._req("POST", "/reset")["links"]
