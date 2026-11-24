"""Traffic tab: the tool drives the deployed Traffic-Gen over its web API (login cookie, X-TG header) with the address
and GUI password it kept at deployment. Only the traffic test (no throughput tests)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402
from app.traffic_client import TrafficClient, TrafficError  # noqa: E402
from tests import fake_trafficgen as ft  # noqa: E402

srv = ft.start(8099)
try:
    # ---- client: logs in on first use and again after a 401, sends X-TG on POSTs
    cl = TrafficClient("127.0.0.1:8099", "Tg-Admin-pw1!", scheme="http")
    assert cl.state()["running"] is False and ft.STATE["logins"] == 1
    cl.start({"duration_minutes": 5, "intensity": "high", "groups": {"collab": 30}})
    assert ft.STATE["started_with"] == {"duration_minutes": 5, "intensity": "high", "groups": {"collab": 30}}
    try:
        cl.start({})
        raise AssertionError("expected TrafficError")
    except TrafficError as e:
        assert "already running" in str(e) and e.status == 400
    cl.s.cookies.clear()                                                        # session expired on Traffic-Gen
    assert cl.state()["running"] is True and ft.STATE["logins"] == 2
    try:
        TrafficClient("127.0.0.1:8099", "wrong", scheme="http").state()
        raise AssertionError("expected TrafficError")
    except TrafficError as e:
        assert "password" in str(e).lower() and e.status == 502
    try:
        TrafficClient("127.0.0.1:1", "x", scheme="http", timeout=2).state()
        raise AssertionError("expected TrafficError")
    except TrafficError as e:
        assert "not reachable" in str(e)
    print("OK  client: login + cookie, X-TG, opnieuw inloggen na 401, duidelijke fouten")

    # ---- API: nothing deployed yet -> 409; after the deployment kept address + password: proxy to Traffic-Gen
    c = TestClient(main.app)
    r = c.get("/api/traffic/status")
    assert r.status_code == 200 and r.json()["deployed"] is False
    assert c.get("/api/traffic/state").status_code == 409
    conn = db.connect(os.environ["DATA_DIR"])
    db.put_secret(conn, "trafficgen_addr", "127.0.0.1:8099")
    db.put_secret(conn, "trafficgen_password", "Tg-Admin-pw1!")
    main.TRAFFIC_SCHEME = "http"                                                 # the fake speaks plain HTTP
    ft.reset()
    st = c.get("/api/traffic/status").json()
    assert st["deployed"] is True and st["url"] == "http://127.0.0.1:8099" and st["reachable"] is True, st
    assert "Tg-Admin-pw1!" not in c.get("/api/traffic/status").text
    assert c.get("/api/traffic/config").json()["groups"]["collab"] == 20
    assert c.get("/api/traffic/apps").json()["collab"][0]["name"] == "Teams"
    r = c.post("/api/traffic/start", json={"duration_minutes": 15, "intensity": "low", "threads_per_ip": 2,
                                           "groups": {"collab": 40, "sdwan": 10}, "evil": 1})
    assert r.status_code == 200 and ft.STATE["started_with"] == {"duration_minutes": 15, "intensity": "low", "threads_per_ip": 2,
                                                                 "groups": {"collab": 40, "sdwan": 10}}, ft.STATE["started_with"]
    s = c.get("/api/traffic/state").json()
    assert s["running"] and s["totals"]["requests"] == 12 and "Teams" in s["apps"]
    assert c.get("/api/traffic/log?n=5").json()[0]["app"] == "Teams"
    assert c.post("/api/traffic/start", json={}).status_code == 400                # Traffic-Gen's own message comes through
    assert c.post("/api/traffic/stop").status_code == 200 and ft.STATE["running"] is False
    r = c.post("/api/traffic/config", json={"groups": {"malware": 5}, "disabled_apps": ["Zoom"]})
    assert r.status_code == 200 and ft.STATE["config"]["groups"] == {"malware": 5} and ft.STATE["config"]["disabled_apps"] == ["Zoom"]
    # throughput / network settings are not passed through
    assert c.post("/api/traffic/tp/start", json={}).status_code in (404, 405)
    print("OK  API: niet uitgerold -> 409; daarna status/config/apps/start/stop/state/log via de tool, wachtwoord nooit terug")
finally:
    srv.shutdown()
print("alle Traffic-tab-tests geslaagd")
