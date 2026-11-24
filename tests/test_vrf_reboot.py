"""Multi-VRF: after the install the FortiGates are rebooted once (via FortiManager -> FortiOS REST API).
Day-0 puts wan1 in VRF 0, the templates then move wan1 and the tunnels to VRF 1; tunnels that already existed keep
sending ESP in VRF 0 ('esp_output4: no route to <spoke>', tx err = tx on the hub; measured on site1-H1). A reboot
builds them in the right VRF."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import fmg as F, model  # noqa: E402


class FakeFmg:
    dry = False

    def __init__(self, seq):
        self.calls, self.seq = [], {n: list(s) for n, s in seq.items()}

    def call(self, method, url, data=None, **kw):
        self.calls.append((method, url, data))
        if method == "get" and "/device/" in url:
            n = url.rsplit("/", 1)[1]
            s = self.seq[n]
            return {"data": {"conn_status": s.pop(0) if len(s) > 1 else s[0]}}
        return {"data": [{"target": "x", "status": {"code": 0}}]}


ev = []
emit = lambda st, s, m: ev.append((s, m))
f = FakeFmg({"h1": ["up", "down", "down", "up"], "s1": ["up", "down", "up"]})
left = F.reboot_devices(f, "SDWAN", ["h1", "s1"], emit, down_timeout=5, up_timeout=5, poll=0)
assert left == [], left
px = [d for m, u, d in f.calls if u == "/sys/proxy/json"]
assert len(px) == 1 and px[0]["action"] == "post" and px[0]["resource"] == "/api/v2/monitor/system/os/reboot"
assert sorted(px[0]["target"]) == ["adom/SDWAN/device/h1", "adom/SDWAN/device/s1"], px
assert any("reboot" in m for _, m in ev) and any("h1: online" in m for _, m in ev), ev
# a device that never comes back: reported, not raised
f = FakeFmg({"h1": ["up", "down"]})
assert F.reboot_devices(f, "SDWAN", ["h1"], emit, down_timeout=2, up_timeout=0.2, poll=0) == ["h1"]
print("OK  reboot via FortiManager-proxy, wacht op offline en weer online, niet-terugkomend device gemeld")

# the pipeline: only for multi-VRF with real FortiGates, after the install
seen = []
F.install_devices = lambda *a, **k: None
F.wait_online = lambda *a, **k: []
F.reboot_devices = lambda fmg, adom, names, emit, **k: seen.append(sorted(names)) or []
devs = [({"name": "h1"}, "hub"), ({"name": "s1"}, "edge")]
for multi, m_fgt, want in ((True, "day0", [["h1", "s1"]]), (False, "day0", []), (True, "none", [])):
    seen.clear()
    t = model.merged({"region": dict(model.DEFAULTS["region"], multi_vrf=multi)})
    F._install_online(None, "SDWAN", {"roles": []}, devs, ["h1", "s1"], {}, emit, m_fgt, t)
    assert seen == want, (multi, m_fgt, seen)
print("OK  pipeline: herstart alleen bij multi-VRF met echte FortiGates")
print("alle VRF-reboot-tests geslaagd")


class Refuse(FakeFmg):
    def call(self, method, url, data=None, **kw):
        if url == "/sys/proxy/json":
            raise F.FmgError("no permission")
        return super().call(method, url, data, **kw)


ev.clear()
import importlib  # noqa: E402
F2 = importlib.reload(F)
assert F2.reboot_devices(Refuse({"h1": ["up"]}), "SDWAN", ["h1"], emit, down_timeout=1, up_timeout=1, poll=0) == ["h1"]
assert any(s == "warn" and "by hand" in m for s, m in ev), ev
print("OK  reboot geweigerd door FortiManager: waarschuwing, uitrol gaat door")
