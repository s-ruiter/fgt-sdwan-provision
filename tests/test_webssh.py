"""SSH in the browser: websocket /api/term/<device> bridges to an SSH shell via the wan-emu port forward."""
import json
import os
import socket
import sys
import tempfile
import threading
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import uvicorn  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from app import db, main, model  # noqa: E402
from app.wanemu import net  # noqa: E402

seen = {}


class Chan:                                       # fake paramiko channel: echoes what it gets
    def __init__(self):
        self.buf, self.closed, self.size = [b"FortiGate login ok\r\nsite1-1 # "], False, None

    def recv_ready(self):
        return bool(self.buf)

    def recv(self, n):
        return self.buf.pop(0) if self.buf else b""

    def send(self, data):
        self.buf.append(b"echo:" + data)
        if data == b"exit\r":
            self.closed = True

    def resize_pty(self, width, height):
        self.size = (width, height)

    def exit_status_ready(self):
        return self.closed

    def close(self):
        self.closed = True


def fake_connect(host, port, user, password, cols, rows):
    seen.update(host=host, port=port, user=user, password=password, cols=cols, rows=rows)
    if password != "goed":
        raise main.TermError("inloggen geweigerd")
    seen["chan"] = Chan()
    return seen["chan"], lambda: None


main.TERM_CONNECT = fake_connect
t = model.merged({})
conn = db.connect(os.environ["DATA_DIR"])
db.put_secret(conn, "wanemu_forwards", json.dumps({"host": "10.10.106.250", "forwards": net.forwards(t)}))
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(100):
    if srv.started:
        break
    time.sleep(0.05)

f = main.wanemu_forwards()
assert f["host"] == "10.10.106.250" and [x["name"] for x in f["forwards"]][:2] == ["site1-H1", "site1-H2"]

with connect(f"ws://127.0.0.1:{port}/api/term/site1-1") as ws:
    ws.send(json.dumps({"user": "admin", "password": "goed", "cols": 120, "rows": 30}))
    first = ws.recv(timeout=3)
    assert "login ok" in first, first
    assert (seen["host"], seen["port"], seen["user"], seen["cols"]) == ("10.10.106.250", 10024, "admin", 120)
    ws.send(json.dumps({"data": "get system status\r"}))
    out = ""
    for _ in range(20):
        out += ws.recv(timeout=3)
        if "echo:get system status" in out:
            break
    assert "echo:get system status" in out, out
    ws.send(json.dumps({"resize": [100, 40]}))
    time.sleep(0.3)
    assert seen["chan"].size == (100, 40)
with connect(f"ws://127.0.0.1:{port}/api/term/site1-1") as ws:          # wrong password: message, then closed
    ws.send(json.dumps({"user": "admin", "password": "fout", "cols": 80, "rows": 24}))
    msg = ws.recv(timeout=3)
    assert "inloggen geweigerd" in msg and "fout" not in msg, msg
with connect(f"ws://127.0.0.1:{port}/api/term/bestaat-niet") as ws:
    ws.send(json.dumps({"user": "admin", "password": "goed"}))
    assert "onbekend" in ws.recv(timeout=3).lower()
srv.should_exit = True
print("OK  SSH in de browser: websocket naar de doorgestuurde SSH-poort, invoer/uitvoer, resize, foute login en onbekend device")
