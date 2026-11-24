# Consoles and several sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Proxmox consoles (LAN clients via the token, FortiGates/FortiManager via a console user) and several minimizable terminal sessions on "Access & WAN control"; SSH stays.

**Architecture:** A new `app/console.py` finds the device in Proxmox, gets a termproxy ticket (LXC: API token; VM: ticket from a console user) and proxies the Proxmox `vncwebsocket` to the browser in the same message format as `/api/term`. The console password is encrypted with Fernet (`app/secretbox.py`). In the frontend the single terminal overlay becomes a session panel with tabs and a minimize bar.

**Tech Stack:** FastAPI websockets, `websockets` (client to Proxmox), `cryptography` (Fernet), xterm.js 5.3.0.

**Spec:** `docs/superpowers/specs/2026-11-24-client-console-multi-ssh-design.md`

## Global Constraints
- The console password is never stored in plain text, never in an export, never in an API response (only `has_console_password`).
- Key file `<DATA_DIR>/console.key`, mode 0600, created on first use.
- LXC consoles: API token. VM consoles: console user (`POST /access/ticket`), because PVE 9.1.4 refuses qemu termproxy to a token.
- Termproxy protocol toward Proxmox: first message `"<user>:<ticket>\n"`, reply `OK`; data `0:<bytelen>:<data>`; resize `1:<cols>:<rows>:`; keepalive `2` every 30 s.
- Browser messages are the same as `/api/term`: start `{cols, rows, user?, password?}`, then `{data}` / `{resize:[c,r]}`; server -> browser text.
- New FortiGate VMs: `serial0: socket`, `vga: serial0`. FortiManager only if the serial test (Task 0) shows a login prompt.
- No `@` handles in commit messages; every commit ends with the Co-Authored-By line.

## Review Focus
- Device stopped / not found in Proxmox -> a red message in the terminal, no hang (test in Task 4).
- VM without serial0 (deployed before this change) -> "no serial port: deploy again, or add serial0 + Display Serial terminal 0 and restart" (Task 3).
- Wrong console password / user without VM.Console -> a clear message, not a stack trace (Task 3).
- Proxmox closes the console (e.g. CT restarts) -> the tab shows "[session ended]" and stays readable (Task 4).
- Opening the same device twice -> the existing tab is shown, not a second session (Task 6).

---

### Task 0: Serial test on the FortiManager (ops, no code)
- [ ] `ssh root@10.10.100.13 'qm set 111 -serial0 socket && qm shutdown 111 --timeout 300 && qm start 111'` (no forceStop). Wait until the FMG answers on https again.
- [ ] `(sleep 2; printf "\r"; sleep 3) | timeout 8 script -qc "qm terminal 111 -iface serial0" /dev/null` -> look for `login:`. Write down the result: FMG_SERIAL = yes/no (decides Task 5).
- [ ] Set `qm set 117 -vga serial0` (and 111 if yes) + clean restart, so the lab VMs can be tested in Task 7.

### Task 1: Encrypted secret box
**Files:** Create `app/secretbox.py`; Modify `requirements.txt` (+`cryptography>=42`); Test `tests/test_console.py` (new).

**Interfaces:** Produces `seal(data_dir: str, text: str) -> str`, `unseal(data_dir: str, token: str) -> str` (raises `ValueError` on a bad token / missing key).

- [ ] Test: `seal` -> a string that does not contain the text; `unseal(seal(x)) == x`; `console.key` exists with `stat.S_IMODE == 0o600`; a second `seal` reuses the same key; `unseal` with another DATA_DIR -> `ValueError`.
- [ ] Run `python tests/test_console.py` -> FAIL (module missing).
- [ ] Implement with `cryptography.fernet.Fernet`; key created with `os.open(..., O_CREAT|O_EXCL, 0o600)`.
- [ ] Run -> PASS. Commit.

### Task 2: Console user per Proxmox server
**Files:** Modify `app/routes_servers.py`, `app/db.py` (helpers); Test `tests/test_console.py`.

**Interfaces:** Consumes Task 1. Produces `db.get_console(conn, sid, data_dir) -> {"user": str, "password": str} | None` (decrypted); stored as secret `pve_console:<sid>` = JSON `{"user", "pw": seal(...)}`. `ServerIn` gets `console_user: Optional[str] = None`, `console_password: Optional[str] = None` (None/"" password = keep; `console_user == ""` clears both). `_out` adds `console_user` and `has_console_password`.

- [ ] Test (TestClient): create a server with console user/password -> GET shows `console_user`, `has_console_password: True` and no password anywhere in the response text; the secret in the DB does not contain the password; `get_console` returns it; PUT without a password keeps it; `console_user: ""` clears it; DELETE of the server removes `pve_console:<sid>`; the settings export (`/api/settings/export`) contains no password.
- [ ] Run -> FAIL. Implement. Run -> PASS. Commit.

### Task 3: Open a Proxmox console (`app/console.py`)
**Files:** Create `app/console.py`; Test `tests/test_console.py` + extend `tests/fake_proxmox.py` (`/cluster/resources`, `POST /access/ticket`, `POST /nodes/{n}/{lxc|qemu}/{id}/termproxy`, `GET /nodes/{n}/qemu/{id}/config`).

**Interfaces:** Produces `class ConsoleError(Exception)`; `locate(pve: Pve, pve_name: str) -> dict(node, kind: "lxc"|"qemu", vmid, status)`; `open_session(row, console: dict|None, pve_name: str) -> dict(ws_url: str, headers: dict, auth_line: str, ssl_verify: bool)`. A VM uses the ticket of `console` (`Cookie: PVEAuthCookie=...`, header `CSRFPreventionToken`); an LXC uses `Authorization: PVEAPIToken=...`.

- [ ] Tests: LXC -> token header, `auth_line == "<user>:<ticket>\n"`, ws_url `wss://.../lxc/<id>/vncwebsocket?port=..&vncticket=..` (url-encoded); VM with a console user -> cookie + CSRF, `/access/ticket` called with that user; errors as `ConsoleError` with these texts: not found -> "not found in Proxmox"; stopped -> "is not running"; VM without a console user -> "add a console user to the Proxmox server"; VM without `serial0` -> "no serial port"; ticket 401 -> "console login refused"; termproxy 403 -> "lacks VM.Console".
- [ ] Run -> FAIL. Implement (requests, the server's verify_tls). Run -> PASS. Commit.

### Task 4: Websocket `/api/console/{name}` + automatic login
**Files:** Modify `app/main.py`; Test `tests/test_console.py` (fake vncwebsocket with `websockets.serve` on 127.0.0.1).

**Interfaces:** Consumes Task 3. `CONSOLE_OPEN = console.open_session` (module hook, like `TERM_CONNECT`). Name -> Proxmox name + login: `@fmg` -> fmg (kept fmg creds); a FortiGate name -> fgt creds; `client-<spoke>` -> `access_creds["client"]` (`{"user": "root", "password": ...}`). Proxmox name = `console_target.prefix + name`; server = `console_target.server_id` (secret `console_target`, written at deployment in Task 5).

- [ ] Tests: auth line sent first, `OK` expected; browser `{data:"ls\r"}` -> fake receives `0:3:ls\r` (byte length, also for UTF-8 `é` -> 2); `{resize:[100,30]}` -> `1:100:30:`; fake sends `login: ` -> tool sends `root\r`, then `Password: ` -> the password + `\r`, exactly once; the password does not reach the browser; a typed password in the start message wins over the kept one; fake closes -> browser receives "[session ended]"; `ConsoleError` -> red message + close.
- [ ] Run -> FAIL. Implement (keepalive task `2` every 30 s; UTF-8 incremental decoder as in `/api/term`). Run -> PASS. Commit.

### Task 5: Deployment keeps what the consoles need; Access lists clients; VM serial
**Files:** Modify `app/main.py` (deploy: `console_target`, `access_creds["client"]`; `/api/access`: `clients` + `console: true`; `/api/access/password`: clients; cleanup clears `client` when the clients are gone), `app/provision_vms.py` (`vm_params`: fgt -> `serial0`, `vga`; fmg the same only if FMG_SERIAL); Tests `tests/test_console.py`, `tests/test_cleanup.py`, `tests/test_vmids.py`.

- [ ] Tests: after a (fake) deployment with clients, `/api/access` has `clients: [{name:"client-site1-1", ip:"10.0.1.10", spoke:"site1-1", password:true}]`; `/api/access/password {"name":"client-site1-1"}` -> root + the password; `console_target` == `{server_id, prefix}`; `vm_params` for fgt contains `serial0 == "socket"` and `vga == "serial0"`; cleanup of the clients removes `client`.
- [ ] Run -> FAIL. Implement. Run all tests (`alltests.sh`) -> PASS. Commit.

### Task 6: Frontend: session panel, minimize bar, Console buttons, console user field
**Files:** Modify `static/app.js` (term section), `static/index.html` (term overlay + server form), `static/style.css`; Test `tests/test_ui_static.py`.

**Interfaces:** `SESS: Map<key, {term, ws, fit, title, kind:"ssh"|"console"}>`, key = `kind + ":" + name`. `sessOpen(kind, name, auto)`, `sessShow(key)`, `sessMin()`, `sessClose(key)`. URL: ssh -> `/api/term/<name>`, console -> `/api/console/<name>`.

- [ ] Static test asserts: `id="sess_tabs"`, `id="sess_bar"`, `id="sess_min"`, `data-console=` in app.js, `/api/console/` in app.js, `srv_console_user` and `srv_console_pw` in index.html, the hint text "encrypted" next to the field.
- [ ] Run -> FAIL. Implement: same device again -> `sessShow`; minimize keeps ws + buffer (container `hidden`, xterm not disposed; `fit()` on show); a closed ws marks the tab "(closed)"; client row: name, spoke, IP, Console, Copy password; FMG/FGT rows get a Console button; server form: Console user + password (type=password, eye toggle as elsewhere), hint "Only for FortiGate/FortiManager consoles; stored encrypted. Advice: its own Proxmox user with only VM.Console."
- [ ] Run -> PASS. Check by hand in the browser pane against a local uvicorn with fakes: 2 sessions, minimize, WAN sliders usable, back. Commit.

### Task 7: Lab verification + PR
- [ ] All tests green (`alltests.sh`). Open a PR (Dutch description in the house style), merge only on "merge en update".
- [ ] After the update of CT 100: the user fills in the console user; check client console (CT 118), FortiGate console (VM 117), FMG console (if FMG_SERIAL), two sessions + minimize.
