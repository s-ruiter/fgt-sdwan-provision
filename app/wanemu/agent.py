#!/usr/bin/env python3
"""WAN-emulator agent: runs inside the wan-emu container (stdlib only, no imports from the tool).

Per link (a FortiGate WAN address on one segment interface) it applies delay / jitter / loss / rate in both
directions with tc, and "link down" plus NAT towards eth0 with nftables.

  to the FortiGate    segment interface (eth1/eth2/eth3): htb root, class per link, netem leaf, u32 on ip dst
  from the FortiGate  ingress of that interface -> mirred to ifb-<iface>, same build-up there, u32 on ip src

Files in the config dir: topology.json (written by the tool: {"links": [...], "nat": [subnets]}), links.json (the
state per link id, kept across restarts) and token (bearer token, mode 0600).
Run: python3 agent.py --conf /etc/wan-emu --listen 10.10.106.250 --port 8090"""
import argparse
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

DEFAULT = {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0.0, "rate_kbit": 0, "down": False}
LIMITS = {"delay_ms": (0, 5000, int), "jitter_ms": (0, 1000, int), "loss_pct": (0, 100, float),
          "rate_kbit": (0, 10_000_000, int)}
FULL = "10gbit"          # rate of an unlimited link / of the default class


def check(values):
    """Validate (part of) a link state; returns the normalised values. Raises ValueError with a readable message."""
    if not isinstance(values, dict):
        raise ValueError("verwacht een JSON-object")
    out = {}
    for k, v in values.items():
        if k == "down":
            if not isinstance(v, bool):
                raise ValueError("down moet true of false zijn")
            out[k] = v
        elif k in LIMITS:
            lo, hi, typ = LIMITS[k]
            try:
                x = typ(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} moet een getal zijn") from None
            if isinstance(v, bool) or not lo <= x <= hi:
                raise ValueError(f"{k} moet tussen {lo} en {hi} liggen")
            out[k] = x
        else:
            raise ValueError(f"onbekend veld {k}")
    return out


def _ifaces(topo):
    return sorted({l["iface"] for l in topo["links"]})


def _ifb(iface):
    return f"ifb-{iface}"


def _netem(s):
    a = ["netem", "delay", f"{s['delay_ms']}ms"]
    if s["jitter_ms"]:
        a.append(f"{s['jitter_ms']}ms")
    if s["loss_pct"]:
        a += ["loss", f"{s['loss_pct']:g}%"]
    return a


def tc_commands(topo, state):
    """All tc commands that build the shaping from scratch (the caller removes the old roots first)."""
    cmds = []
    for iface in _ifaces(topo):
        ifb = _ifb(iface)
        for dev in (iface, ifb):
            cmds += [["tc", "qdisc", "replace", "dev", dev, "root", "handle", "1:", "htb", "default", "9999"],
                     ["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:9999", "htb", "rate", FULL]]
        cmds += [["tc", "qdisc", "replace", "dev", iface, "handle", "ffff:", "ingress"],
                 ["tc", "filter", "add", "dev", iface, "parent", "ffff:", "matchall", "action", "mirred", "egress",
                  "redirect", "dev", ifb]]
    for n, link in enumerate(topo["links"], start=10):
        s = dict(DEFAULT, **state.get(link["id"], {}))
        rate = f"{s['rate_kbit']}kbit" if s["rate_kbit"] else FULL
        for dev, field in ((link["iface"], "dst"), (_ifb(link["iface"]), "src")):
            cmds += [["tc", "class", "add", "dev", dev, "parent", "1:", "classid", f"1:{n}", "htb", "rate", rate,
                      "ceil", rate],
                     ["tc", "qdisc", "add", "dev", dev, "parent", f"1:{n}", "handle", f"{n}:", *_netem(s)],
                     ["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ip", "prio", "1", "u32",
                      "match", "ip", field, f"{link['ip']}/32", "flowid", f"1:{n}"]]
    return cmds


def nft_ruleset(topo, state):
    """Own table inet wanemu, replaced as a whole: forward drops for links that are down, NAT to the lab on eth0."""
    drops = []
    for link in topo["links"]:
        if state.get(link["id"], {}).get("down"):
            drops += [f"    ip saddr {link['ip']} drop", f"    ip daddr {link['ip']} drop"]
    nat = ", ".join(topo.get("nat", []))
    lines = ["table inet wanemu", "delete table inet wanemu", "table inet wanemu {",
             "  chain forward {", "    type filter hook forward priority 0; policy accept;", *drops, "  }",
             "  chain postrouting {", "    type nat hook postrouting priority 100; policy accept;"]
    if nat:
        lines.append(f'    oifname "eth0" ip saddr {{ {nat} }} masquerade')
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


class App:
    def __init__(self, conf_dir, token, run=subprocess.run):
        self.dir, self.token, self.run, self.lock = conf_dir, token, run, Lock()
        with open(os.path.join(conf_dir, "topology.json")) as f:
            self.topo = json.load(f)
        self.ids = {l["id"] for l in self.topo["links"]}
        try:
            with open(os.path.join(conf_dir, "links.json")) as f:
                self.state = {k: v for k, v in json.load(f).items() if k in self.ids}
        except (OSError, ValueError):
            self.state = {}

    def _save(self):
        p = os.path.join(self.dir, "links.json")
        with open(p + ".tmp", "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(p + ".tmp", p)

    def _sh(self, cmd, check=True, input=None):
        r = self.run(cmd, input=input, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout or '').strip()}")
        return r

    def apply(self):
        for iface in _ifaces(self.topo):
            ifb = _ifb(iface)
            if self._sh(["ip", "link", "show", ifb], check=False).returncode != 0:
                self._sh(["ip", "link", "add", ifb, "type", "ifb"])
            self._sh(["ip", "link", "set", ifb, "up"])
            for dev in (iface, ifb):
                self._sh(["tc", "qdisc", "del", "dev", dev, "root"], check=False)
            self._sh(["tc", "qdisc", "del", "dev", iface, "ingress"], check=False)
        for c in tc_commands(self.topo, self.state):
            self._sh(c)
        self._sh(["nft", "-f", "-"], input=nft_ruleset(self.topo, self.state))

    def view(self):
        return [dict(l, state=dict(DEFAULT, **self.state.get(l["id"], {}))) for l in self.topo["links"]]

    def put(self, link_id, values):
        vals = check(values)                                    # ValueError -> 400, nothing changed
        with self.lock:
            old = dict(self.state)
            self.state[link_id] = dict(DEFAULT, **self.state.get(link_id, {}), **vals)
            try:
                self.apply()
            except Exception:
                self.state = old
                raise
            self._save()
            return self.state[link_id]

    def reset(self):
        with self.lock:
            self.state = {}
            self.apply()
            self._save()

    def make_server(self, host, port):
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data):
                b = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _auth(self):
                if self.headers.get("Authorization") != f"Bearer {app.token}":
                    self._send(401, {"error": "token ontbreekt of is onjuist"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                if self.path == "/links":
                    return self._send(200, {"links": app.view()})
                self._send(404, {"error": "onbekend pad"})

            def do_PUT(self):
                if not self._auth():
                    return
                lid = self.path[len("/links/"):] if self.path.startswith("/links/") else None
                if lid not in app.ids:
                    return self._send(404, {"error": f"onbekende link {lid}"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    return self._send(200, {"id": lid, "state": app.put(lid, body)})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:                       # tc / nft refused: state rolled back
                    return self._send(500, {"error": str(e)})

            def do_POST(self):
                if not self._auth():
                    return
                if self.path != "/reset":
                    return self._send(404, {"error": "onbekend pad"})
                try:
                    app.reset()
                    self._send(200, {"links": app.view()})
                except Exception as e:
                    self._send(500, {"error": str(e)})

        return ThreadingHTTPServer((host, port), H)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", default="/etc/wan-emu")
    ap.add_argument("--listen", required=True)
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    with open(os.path.join(a.conf, "token")) as f:
        token = f.read().strip()
    app = App(a.conf, token)
    app.apply()                                                 # restore the saved state after a (re)start
    app.make_server(a.listen, a.port).serve_forever()


if __name__ == "__main__":
    main()
