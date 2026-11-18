"""WAN-emulator API in the tool: proxy to the agent (token stays in the tool), presets, clear errors."""
import json
import os
import sys
import tempfile
import threading

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi import HTTPException  # noqa: E402

from app import db, main, model  # noqa: E402
from app.wanemu import agent, client, net  # noqa: E402

t = model.merged({})
d = tempfile.mkdtemp()
json.dump(net.agent_topology(t), open(os.path.join(d, "topology.json"), "w"))


class R:
    returncode, stdout, stderr = 0, "", ""


app = agent.App(d, "geheimtoken123", run=lambda *a, **k: R())
srv = app.make_server("127.0.0.1", 0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]


def err(fn, *a):
    try:
        fn(*a)
    except HTTPException as e:
        return e.status_code, str(e.detail)
    raise SystemExit(f"{fn.__name__} gaf geen fout")


# nothing deployed yet
code, msg = err(main.wanemu_links)
assert code == 409 and "nog geen wan-emulator" in msg.lower(), msg
conn = db.connect(os.environ["DATA_DIR"])
db.put_secret(conn, "wanemu_agent_token", "geheimtoken123")
db.put_secret(conn, "wanemu_addr", f"127.0.0.1:{port}")

r = main.wanemu_links()
assert len(r["links"]) == 7 and r["addr"] == f"127.0.0.1:{port}" and "slechte-4g" in r["presets"]
assert {l["seg"] for l in r["links"]} == {"hub", "a", "b"} and all("state" in l for l in r["links"])
assert "geheimtoken123" not in json.dumps(r)
r = main.wanemu_put("site1-1-wan1", {"delay_ms": 25, "loss_pct": 1})
assert r["state"]["delay_ms"] == 25 and app.state["site1-1-wan1"]["loss_pct"] == 1
code, msg = err(main.wanemu_put, "site1-1-wan1", {"loss_pct": 150})
assert code == 400 and "loss_pct" in msg
r = main.wanemu_preset("isp-a-storing", None)
down = sorted(k for k, v in app.state.items() if v.get("down"))
assert down == ["site1-1-wan1", "site1-2-wan1", "site1-3-wan1"], down
main.wanemu_preset("slechte-4g", "site1-3-wan2")
assert app.state["site1-3-wan2"]["delay_ms"] == 60 and app.state.get("site1-1-wan2", {}).get("delay_ms", 0) == 0
main.wanemu_preset("hub1-weg", None)
assert app.state["site1-H1-wan1"]["down"] is True
code, _ = err(main.wanemu_preset, "bestaat-niet", None)
assert code == 404
main.wanemu_reset()
assert app.state == {}
print("OK  WAN-emulatie-API: proxy met token in de tool, PUT, ongeldige waarde 400, presets (storing/hub weg/per link), reset")

srv.shutdown()
srv.server_close()
code, msg = err(main.wanemu_links)
assert code == 502 and f"WAN-emulator niet bereikbaar op 127.0.0.1:{port}" in msg
db.put_secret(conn, "wanemu_agent_token", "fout")
print("OK  agent onbereikbaar: 502 met duidelijke melding")
