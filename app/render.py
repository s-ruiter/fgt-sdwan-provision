"""Render per-device FortiOS configs from the Fortinet Jinja Orchestrator."""
import json
import os
from pathlib import Path
import jinja2
from .ipfilter import ipaddr

FLAVOR = "dynamic-bgp-on-lo"


def render_inventory(orch_dir, project_text, inventory, skip_optional=False):
    """Return {device: config_text}. `inventory` = {'defaults':{}, 'Hub':{}, 'Edge':{}}."""
    flavor_dir = Path(orch_dir) / FLAVOR
    loader = jinja2.DictLoader({"00-Project": project_text})
    fs = jinja2.FileSystemLoader(str(flavor_dir))
    env = jinja2.Environment(loader=jinja2.ChoiceLoader([loader, fs]),
                             undefined=jinja2.StrictUndefined)
    env.filters["ipaddr"] = ipaddr
    inv = json.loads(json.dumps(inventory))
    defaults = inv.pop("defaults", {})
    out = {}
    for group, devs in inv.items():
        names = sorted(f for f in os.listdir(flavor_dir) if group in f and f.endswith(".j2"))
        if not skip_optional:
            names += sorted("optional/" + f for f in os.listdir(flavor_dir / "optional") if group in f)
        names = sorted(names, key=lambda n: Path(n).name)
        for dev, meta in devs.items():
            parts = ["execute batch start"]
            for n in names:
                r = env.get_template(n).render({**defaults, **meta})
                body = "\n".join(l for l in r.split("\n") if l.strip())
                parts += ["#" * 38, "# " + n, "#" * 38, body, ""]
            parts.append("execute batch end")
            out[dev] = "\n".join(parts) + "\n"
    return out
