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
# one interface per link (site1-1 wan1 = eth3): a root htb with one class + netem leaf, both directions (ifb)
assert "tc qdisc replace dev eth3 root handle 1: htb default 1" in s
assert "tc qdisc replace dev ifb-eth3 root handle 1: htb default 1" in s
assert "tc qdisc replace dev eth3 handle ffff: ingress" in s
assert any("mirred egress redirect dev ifb-eth3" in x and "dev eth3" in x for x in s)
assert any("netem delay 40ms 5ms loss 1%" in x and "dev eth3 " in x for x in s)
assert any("netem delay 40ms 5ms loss 1%" in x and "dev ifb-eth3 " in x for x in s)
assert any("rate 20000kbit" in x and "dev eth3 " in x for x in s) and any("rate 10gbit" in x and "dev eth1 " in x for x in s)
assert not any("u32" in x for x in s), "no IP filters: the interface is the link"
assert {x.split()[4] for x in s if x.startswith("tc qdisc replace") and "root" in x} == \
    {f"eth{k}" for k in range(1, 8)} | {f"ifb-eth{k}" for k in range(1, 8)}
r0 = agent.nft_ruleset(topo, {})
assert 'oifname "eth0"' in r0 and "masquerade" in r0 and "100.65.0.0/16" in r0 and " drop" not in r0
r1 = agent.nft_ruleset(topo, {"site1-1-wan1": dict(agent.DEFAULT, down=True)})
assert 'iifname "eth3" drop' in r1 and 'oifname "eth3" drop' in r1
for bad in ({"loss_pct": 150}, {"delay_ms": -1}, {"jitter_ms": 2000}, {"rate_kbit": "x"}, {"foo": 1}, {"down": "ja"}):
    try:
        agent.check(bad)
        raise SystemExit(f"check accepteerde {bad}")
    except ValueError:
        pass
assert agent.check({"loss_pct": "2.5", "delay_ms": 10}) == {"loss_pct": 2.5, "delay_ms": 10}
print("OK  agent: per link een interface met htb+netem in beide richtingen (ifb), NAT op eth0, link down per interface")

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
assert 'oifname "eth0" ip saddr { 100.64.0.0/16, 100.65.0.0/16, 100.66.0.0/16 } ip daddr != { 10.10.106.0/24 } masquerade' in r, r
print("OK  agent: geen NAT naar het lab-subnet (FortiManager ziet de echte adressen)")
