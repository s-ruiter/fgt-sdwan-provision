"""Fake FortiOS/FortiManager SSH CLI for testing app/bootstrap.py over a REAL SSH transport (paramiko).
Emulates: forced password change on a blank-password first login, config blocks, `execute vm-license`
with (y/n) + disconnect (reboot), a rejected token, and `get system status`.
It is a behavioural stand-in written from documentation - NOT a FortiOS."""
import socket
import threading

import paramiko

BAD_TOKEN = "BADTOKEN12345"


class State:
    def __init__(self, serial="FGVMMLTM26000001"):
        self.serial = serial
        self.password = ""          # blank = factory
        self.cmds = []
        self.licensed = False
        self.reboots = 0


class Srv(paramiko.ServerInterface):
    def __init__(self, state):
        self.state = state
        self.ev = threading.Event()

    def check_auth_password(self, user, pw):
        return paramiko.AUTH_SUCCESSFUL if pw == self.state.password else paramiko.AUTH_FAILED

    def get_allowed_auths(self, user):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *a):
        return True

    def check_channel_shell_request(self, channel):
        self.ev.set()
        return True


def _readline(ch):
    buf = ""
    while True:
        c = ch.recv(1)
        if not c:
            return None
        c = c.decode()
        if c in "\r\n":
            return buf
        buf += c


def _session(ch, st):
    host = "FGVM-test"
    if st.password == "":
        ch.send("You are forced to change your password. Please input a new password.\r\nNew Password:")
        a = _readline(ch)
        ch.send("\r\nConfirm Password:")
        b = _readline(ch)
        if a is None or a != b:
            ch.close()
            return
        st.password = a
        st.cmds.append("<password changed>")
    depth = []
    ch.send(f"\r\n{host} # ")
    while True:
        line = _readline(ch)
        if line is None:
            return
        line = line.strip()
        if line:
            st.cmds.append(line)
        reply = ""
        if line.startswith("config "):
            depth.append(line.split()[-1])
        elif line == "end" and depth:
            depth.pop()
        elif line.startswith("execute vm-license"):
            tok = line.split()[-1]
            if tok == BAD_TOKEN:
                reply = "\r\nLicense invalid or already used.\r\n"
            else:
                ch.send("\r\nThis operation will reboot the system !\r\nDo you want to continue? (y/n)")
                ans = _readline(ch)
                st.cmds.append(ans)
                if ans == "y":
                    st.licensed = True
                    st.reboots += 1
                    ch.send("\r\nVM license install succeeded. Rebooting firewall.\r\n")
                    ch.close()
                    return
        elif line == "get system status":
            reply = (f"\r\nVersion: FortiGate-VM64 v8.0.0,build0001\r\nSerial-Number: {st.serial}\r\n"
                     f"License Status: {'Valid' if st.licensed else 'Invalid'}\r\n")
        elif line.startswith("bogus"):
            reply = "\r\nCommand fail. Return code -1\r\n"
        prompt = f"{host} ({depth[-1]}) # " if depth else f"{host} # "
        ch.send(reply + f"\r\n{prompt}" if not reply.endswith("\r\n") else reply + prompt)


class FakeDevice:
    def __init__(self, state=None):
        self.state = state or State()
        self.key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        t = paramiko.Transport(c)
        t.add_server_key(self.key)
        srv = Srv(self.state)
        try:
            t.start_server(server=srv)
            ch = t.accept(20)
            if ch is None or not srv.ev.wait(10):
                return
            _session(ch, self.state)
            import time
            time.sleep(0.2)
            ch.close()
            t.close()
        except Exception:
            pass

    def stop(self):
        self.running = False
        self.sock.close()
