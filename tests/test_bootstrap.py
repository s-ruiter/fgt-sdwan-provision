"""Bootstrap tests against the fake SSH FortiOS. Run: python tests/test_bootstrap.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import model
import labuplink  # noqa: E402,F401  (fills in the lab uplink: it has no default)
from app.bootstrap import (BootstrapError, Masker, Timing, bootstrap_device, fgt_network_lines,
                           fmg_network_lines, ssh_connect)
from tests.fake_fortios import BAD_TOKEN, FakeDevice, State

TOKEN = "A1B2C3D4E5F6G7H8I9J0"
FAST = Timing(cmd=10, login=10, reboot_down=1, reboot_up=5, link_up=5)


def topo():
    t = model.merged({})
    t["dns_primary"] = "1.1.1.1"
    t["fmgr"].update(ip="10.0.0.5/24", gateway="10.0.0.1")
    return t


def run(dev_def, kind="fgt", token=TOKEN, new_pw="N3w-Pass!", initial_ip="192.168.1.99", target=None,
        lines=None, serial="FGVMMLTM26000001", expect_serial="FGVMMLTM26000001"):
    dev = FakeDevice(State(serial))
    log = []
    mask = Masker(token, new_pw)

    def connector(host, user, pw):     # every address maps onto the one fake device
        return ssh_connect("127.0.0.1", user, pw, port=dev.port, mask=mask)
    try:
        st = bootstrap_device(kind, dev_def["name"], lines, initial_ip, target or initial_ip, token,
                              {"user": "admin", "initial_pw": "", "new_pw": new_pw},
                              lambda lvl, m: log.append((lvl, m)), connector=connector,
                              waiter=lambda h, p, up, t: True, timing=FAST, expect_serial=expect_serial)
        return dev, log, st, None
    except BootstrapError as e:
        return dev, log, None, e
    finally:
        dev.stop()


def test_fgt_dhcp():
    t = topo()
    d = t["edges"][0]
    dev, log, st, err = run(d, lines=fgt_network_lines(d, t))
    assert err is None, err
    c = dev.state.cmds
    assert "<password changed>" in c, "forced password change niet afgehandeld"
    assert "set mode dhcp" in c and 'set fmg "10.0.0.5"' in c and "set primary 1.1.1.1" in c
    assert f"execute vm-license {TOKEN}" in c and "y" in c and dev.state.licensed and dev.state.reboots == 1
    assert st["license"] == "Valid" and st["serial"] == "FGVMMLTM26000001"
    assert not any(TOKEN in m or "N3w-Pass!" in m for _, m in log), log
    print("OK  FortiGate DHCP: pw-wissel, config, vm-license (y/n), reboot, verify; token niet in log")


def test_fgt_static_with_ip_move():
    t = topo()
    d = t["hubs"][0]
    d.update(wan1_gw="100.127.200.1")
    lines = fgt_network_lines(d, t)
    assert lines[-4:] == ["set allowaccess ping https ssh fgfm", "set ip 100.127.200.11 255.255.255.0", "next", "end"], lines[-6:]
    dev, log, st, err = run(d, lines=lines, target="100.127.200.11")
    assert err is None, err
    c = dev.state.cmds
    assert "set mode static" in c and "set gateway 100.127.200.1" in c
    assert 'set sdwan-zone "underlay"' in c and "set device" not in c     # default via SD-WAN, not a route on port1
    assert any("waiting for 100.127.200.11" in m for _, m in log)
    print("OK  FortiGate static: route + IP als laatste, herverbinden op nieuw IP, licentie")


def test_bad_token():
    t = topo()
    d = t["edges"][0]
    dev, log, st, err = run(d, token=BAD_TOKEN, lines=fgt_network_lines(d, t))
    assert err and "licence refused" in str(err), err
    assert BAD_TOKEN not in str(err)
    print("OK  afgewezen token -> duidelijke fout, token gemaskeerd")


def test_serial_mismatch():
    t = topo()
    d = t["edges"][0]
    dev, log, st, err = run(d, lines=fgt_network_lines(d, t), serial="FGVMMLTM26009999")
    assert err and "differs" in str(err), err
    print("OK  serienummer-mismatch gevangen")


def test_no_new_password():
    t = topo()
    d = t["edges"][0]
    dev, log, st, err = run(d, lines=fgt_network_lines(d, t), new_pw="")
    assert err and "new password" in str(err), err
    print("OK  forced pw-change zonder nieuw wachtwoord -> duidelijke fout")


def test_fmg():
    t = topo()
    lines = fmg_network_lines(t)
    assert "set ip 10.0.0.5 255.255.255.0" in lines and "set gateway 10.0.0.1" in lines
    assert lines[-3:] == ["set ip 10.0.0.5 255.255.255.0", "next", "end"]
    dev, log, st, err = run({"name": "fmg"}, kind="fmg", lines=lines, expect_serial=None)
    assert err is None, err
    assert f"execute vm-license {TOKEN}" in dev.state.cmds
    print("OK  FortiManager: vast IP/route/DNS + vm-license")


def test_same_password_when_not_factory():
    """Device already has the admin password: login with blank fails, then the single user password is used."""
    t = topo()
    d = t["edges"][0]
    dev = FakeDevice(State("FGVMMLTM26000001"))
    dev.state.password = "N3w-Pass!"
    mask = Masker(TOKEN, "N3w-Pass!")
    try:
        st = bootstrap_device("fgt", d["name"], fgt_network_lines(d, t), "192.168.1.99", "192.168.1.99", TOKEN,
                              {"user": "admin", "initial_pw": "", "new_pw": "N3w-Pass!"}, lambda *_: None,
                              connector=lambda h, u, p: ssh_connect("127.0.0.1", u, p, port=dev.port, mask=mask),
                              waiter=lambda h, p, up, tmo: True, timing=FAST, expect_serial="FGVMMLTM26000001")
    finally:
        dev.stop()
    assert dev.state.licensed and st["license"] == "Valid" and "<password changed>" not in dev.state.cmds
    print("OK  bestaand wachtwoord: een veld voor admin en SSH volstaat (geen factory-login nodig)")


def test_token_validation():
    assert model.valid_token(TOKEN)
    for bad in ("", "short", "abc; config system admin", "AAAAAAAAAA\nexecute shutdown"):
        assert not model.valid_token(bad), bad
    print("OK  token-validatie weigert syntax/injectie")


if __name__ == "__main__":
    for f in (test_same_password_when_not_factory, test_token_validation, test_fgt_dhcp, test_fgt_static_with_ip_move, test_bad_token,
              test_serial_mismatch, test_no_new_password, test_fmg):
        f()
    print("alle bootstrap-tests geslaagd")
