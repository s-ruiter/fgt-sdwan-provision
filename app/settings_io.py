"""Settings file (JSON) and saved settings: the whole topology, so a deployment can be rebuilt from it.

The Proxmox server travels by name (the id is local to one tool); its URL, token id and secret never go into the file.
No passwords or FortiFlex tokens either: they are not part of the topology. Fixed values are enforced again on import
by model.merged."""
import copy

from . import model

FORMAT = "fgt-sdwan-settings"
VERSION = 1
PVE_LOCAL = ("server_id", "url", "token_id", "verify_tls")       # belong to the saved server, not to the settings


class SettingsError(ValueError):
    pass


def to_file(topology, server_name="", ui=None):
    """{format, version, ui, topology}: topology merged (fixed values, WAN design), server by name."""
    t = copy.deepcopy(model.merged(topology))
    for k in PVE_LOCAL:
        t["pve"].pop(k, None)
    t["pve"]["server_name"] = server_name or ""
    return {"format": FORMAT, "version": VERSION, "ui": dict(ui or {}), "topology": t}


def from_file(data, servers):
    """data: a settings file (dict). servers: {name: id} of this tool. Returns (topology, ui, notes)."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SettingsError("not a settings file of this tool (format fgt-sdwan-settings)")
    if data.get("version") != VERSION:
        raise SettingsError(f"settings file version {data.get('version')} is not supported (expected {VERSION})")
    t = data.get("topology")
    if not isinstance(t, dict) or not t:
        raise SettingsError("settings file has no topology")
    t = copy.deepcopy(t)
    pve = t.get("pve") if isinstance(t.get("pve"), dict) else {}
    name = str(pve.pop("server_name", "") or "")
    for k in PVE_LOCAL:
        pve.pop(k, None)
    notes = []
    sid = servers.get(name) if name else None
    if name and sid is None:
        notes.append(f"Proxmox server '{name}' does not exist in this tool: choose a server")
    pve["server_id"] = sid
    t["pve"] = pve
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    return model.merged(t), ui, notes


def summary(topology):
    t = model.merged(topology)
    return {"adom": t.get("adom", ""), "devices": len(t.get("hubs", [])) + len(t.get("edges", []))}
