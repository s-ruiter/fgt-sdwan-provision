"""FortiManager system settings (idle timeouts, timezone, ADOM advanced mode, FortiAnalyzer features + reboot) against
the mock FMG, set to the factory values read from a real FMG 8.0.2.  python tests/test_fmg_settings.py"""
import os
import sys
import threading
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.fmg import FMG_SETTINGS, Fmg, FmgError, apply_fmg_settings  # noqa: E402
from tests import mock_fmg  # noqa: E402

srv = HTTPServer(("127.0.0.1", 8102), mock_fmg.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:8102"
G, A = "cli/global/system/global", "cli/global/system/admin/setting"
mock_fmg.STATE["cli"] = {                                   # factory values of the real FMG 8.0.2
    G: {"adom-mode": "normal", "adom-status": "enable", "faz-status": "disable", "timezone": "04"},
    A: {"idle_timeout": 900, "idle_timeout_api": 900, "idle_timeout_gui": 900, "idle_timeout_sso": 900}}
waits = []


def waiter(host, port, up, timeout):
    waits.append((host, port, up))
    return True


def run(**kw):
    ev = []
    f = Fmg(URL, "admin", "pw")
    f.login()
    boot = {"waiter": waiter, "login_delay": 0, **kw}
    apply_fmg_settings(f, boot, lambda s, st, m: ev.append((s, st, m)))
    return ev


# 1. first run: everything set, FortiAnalyzer last, reboot waited out, login again
mock_fmg.STATE["cli_sets"] = []
ev = run()
cli = mock_fmg.STATE["cli"]
assert all(cli[A][k] == 10800 for k in ("idle_timeout", "idle_timeout_api", "idle_timeout_gui", "idle_timeout_sso")), cli[A]
assert cli[G]["timezone"] == "28" and cli[G]["adom-mode"] == "advanced" and cli[G]["faz-status"] == "enable", cli[G]
sets = mock_fmg.STATE["cli_sets"]
assert sets[-1] == (G, {"faz-status": "enable"}), sets                 # FAZ (reboot) is the very last change
assert not any("adom-status" in d for _, d in sets), sets             # already enabled: not sent again
assert waits == [("127.0.0.1", 8102, False), ("127.0.0.1", 8102, True)], waits
assert ev[-1][1] == "ok" and "FortiAnalyzer features on" in ev[-1][2], ev[-1]
print("OK  eerste run: timers 180 min (10800 s), tijdzone 28, ADOM advanced, FAZ aan als laatste + herstart afgewacht")

# 2. second run: nothing to change, no reboot
mock_fmg.STATE["cli_sets"], waits[:] = [], []
ev = run()
assert mock_fmg.STATE["cli_sets"] == [] and waits == [], (mock_fmg.STATE["cli_sets"], waits)
assert ev[-1][1] == "ok" and "were already on" in ev[-1][2]
print("OK  tweede run: niets gewijzigd, geen herstart (idempotent)")

# 3. FMG does not come back -> clear error
cli[G]["faz-status"] = "disable"
try:
    ev = []
    f = Fmg(URL, "admin", "pw")
    f.login()
    apply_fmg_settings(f, {"waiter": lambda h, p, up, t: not up}, lambda *a: ev.append(a))
    raise AssertionError("moest falen")
except FmgError as e:
    assert "does not come back" in str(e), e
print("OK  FortiManager komt niet terug na FAZ-herstart: duidelijke fout")

# 4. dry-run sends nothing
mock_fmg.STATE["cli_sets"] = []
ev = []
apply_fmg_settings(Fmg(URL, "admin", "pw", dry_run=True), {}, lambda *a: ev.append(a))
assert mock_fmg.STATE["cli_sets"] == [] and len(ev) == len(FMG_SETTINGS) + 1
print("OK  dry-run: alleen gelogd")
print("alle FMG-instellingen-tests geslaagd")
