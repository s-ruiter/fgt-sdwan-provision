"""Fake SSH shell into the wan-emu container: records commands and uploaded files. NOT a real container."""


class FakeShell:
    def __init__(self, fail=(), installed=False):
        self.cmds, self.files, self.fail, self.installed, self.closed = [], {}, tuple(fail), installed, False

    def run(self, cmd, input=None):
        self.cmds.append(cmd)
        for f in self.fail:
            if f in cmd:
                return 1, "", f"simulated failure: {f}"
        if cmd.startswith("dpkg -s") and not self.installed:
            return 1, "", "not installed"
        return 0, "", ""

    def put(self, path, content, mode=0o644):
        self.files[path] = (content, mode)

    def close(self):
        self.closed = True


def connector(shell):
    seen = []

    def c(host, pem):
        seen.append((host, pem))
        return shell
    c.seen = seen
    return c
