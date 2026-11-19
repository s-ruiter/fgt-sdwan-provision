"""WAN-emulator agent: tc/nft commands from a link state, HTTP API with token, validation, persistence."""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import model  # noqa: E402
import labuplink  # noqa: E402,F401  (fills in the lab uplink: it has no default)
from app.wanemu import agent, net  # noqa: E402

topo = net.agent_topology(model.merged({}))
st = {"site1-1-wan1": dict(agent.DEFAULT, delay_ms=40, jitter_ms=5, loss_pct=1, rate_kbit=20000)}
s = [" ".join(c) for c in agent.tc_commands(topo, st)]
assert "tc qdisc replace dev eth2 root handle 1: htb default 9999" in s
assert "tc qdisc replace dev ifb-eth2 root handle 1: htb default 9999" in s
assert "tc qdisc replace dev eth2 handle ffff: ingress" in s
assert any("mirred egress redirect dev ifb-eth2" in x and "dev eth2" in x for x in s)
assert any("netem delay 40ms 5ms loss 1%" in x and "dev eth2" in x for x in s)
assert any("netem delay 40ms 5ms loss 1%" in x and "dev ifb-eth2" in x for x in s)
assert any("match ip dst 100.127.201.11/32" in x and "dev eth2" in x for x in s)
assert any("match ip src 100.127.201.11/32" in x and "dev ifb-eth2" in x for x in s)
assert any("rate 20000kbit" in x for x in s) and any("rate 10gbit" in x for x in s)
assert {x.split()[4] for x in s if x.startswith("tc qdisc replace") and "root" in x} == {"eth1", "eth2", "eth3", "ifb-eth1", "ifb-eth2", "ifb-eth3"}
r0 = agent.nft_ruleset(topo, {})
assert 'oifname "eth0"' in r0 and "masquerade" in r0 and "100.127.201.0/24" in r0 and " drop" not in r0
r1 = agent.nft_ruleset(topo, {"site1-1-wan1": dict(agent.DEFAULT, down=True)})
assert "ip saddr 100.127.201.11 drop" in r1 and "ip daddr 100.127.201.11 drop" in r1
for bad in ({"loss_pct": 150}, {"delay_ms": -1}, {"jitter_ms": 2000}, {"rate_kbit": "x"}, {"foo": 1}, {"down": "ja"}):
    try:
        agent.check(bad)
        raise SystemExit(f"check accepteerde {bad}")
    except ValueError:
        pass
assert agent.check({"loss_pct": "2.5", "delay_ms": 10}) == {"loss_pct": 2.5, "delay_ms": 10}
print("OK  agent: htb+netem per link in beide richtingen (ifb), NAT op eth0, link down via nft, waarden gecontroleerd")

# HTTP API
calls = []


class R:
    returncode, stdout, stderr = 0, "", ""


def fake(cmd, **kw):
    calls.append((cmd, kw.get("input")))
    return R()


d = tempfile.mkdtemp()
with open(os.path.join(d, "topology.json"), "w") as f:
    json.dump(topo, f)
app = agent.App(d, "tok", run=fake)
srv = app.make_server("127.0.0.1", 0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def req(method, path, body=None, token="tok"):
    rq = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(rq) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


assert req("GET", "/links", token=None)[0] == 401 and req("GET", "/links", token="nee")[0] == 401
code, body = req("GET", "/links")
assert code == 200 and len(body["links"]) == 7 and body["links"][0]["state"] == agent.DEFAULT
code, body = req("PUT", "/links/site1-1-wan1", {"delay_ms": 30, "loss_pct": 2})
assert code == 200 and body["state"]["delay_ms"] == 30
assert any(c[0][:2] == ["nft", "-f"] for c in calls) and any(c[0][0] == "tc" for c in calls)
code, body = req("PUT", "/links/site1-1-wan1", {"loss_pct": 150})
assert code == 400 and "loss" in body["error"]
assert next(x for x in req("GET", "/links")[1]["links"] if x["id"] == "site1-1-wan1")["state"]["loss_pct"] == 2
code, body = req("PUT", "/links/site1-1-wan1", {"delay_ms": 35, "down": True})            # same keys again: merged
assert code == 200 and body["state"]["delay_ms"] == 35 and body["state"]["loss_pct"] == 2 and body["state"]["down"] is True
req("PUT", "/links/site1-1-wan1", {"delay_ms": 30, "down": False})
assert req("PUT", "/links/onbekend", {"delay_ms": 1})[0] == 404
saved = json.load(open(os.path.join(d, "links.json")))
assert saved["site1-1-wan1"]["delay_ms"] == 30
app2 = agent.App(d, "tok", run=fake)                                               # restart: state read back
assert app2.state["site1-1-wan1"]["loss_pct"] == 2
assert req("POST", "/reset")[0] == 200 and json.load(open(os.path.join(d, "links.json"))) == {}
srv.shutdown()
print("OK  agent-API: token verplicht, PUT past toe en bewaart, ongeldige waarde 400 zonder wijziging, herstart leest terug, reset")

# start-up: the uplink address may not exist yet when systemd starts the agent (errno 99) -> wait, don't crash
tries = []


class Srv:
    pass


def mk(host, port):
    tries.append(1)
    if len(tries) < 3:
        raise OSError(99, "Cannot assign requested address")
    return Srv()


assert isinstance(agent.bind_retry(mk, "10.10.106.250", 8090, timeout=5, delay=0), Srv) and len(tries) == 3
try:
    agent.bind_retry(lambda h, p: (_ for _ in ()).throw(OSError(98, "in use")), "x", 1, timeout=5, delay=0)
    raise SystemExit("andere OSError hoort door te gaan")
except OSError as e:
    assert e.errno == 98
print("OK  agent wacht bij opstarten op zijn uplink-adres (errno 99) in plaats van te crashen")
r = agent.nft_ruleset(dict(topo, no_nat=["10.10.106.0/24"]), {})
assert 'oifname "eth0" ip saddr { 100.127.200.0/24, 100.127.201.0/24, 100.127.202.0/24 } ip daddr != { 10.10.106.0/24 } masquerade' in r, r
print("OK  agent: geen NAT naar het lab-subnet (FortiManager ziet de echte adressen)")
