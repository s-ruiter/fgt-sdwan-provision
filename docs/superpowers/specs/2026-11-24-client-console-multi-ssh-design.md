# Client consoles and several SSH sessions on "Access & WAN control"

## Why
The tool should work as a stand-alone demo tool: everything from "Access & WAN control", with no Proxmox GUI. Two gaps:
1. Only one web-SSH session at a time. To use the WAN sliders you have to close a session (for example a running flow
   trace on site1-1).
2. The LAN clients (`client-site1-N`) can only be reached through the Proxmox console.

## Goals
- Several terminal sessions open at once (FortiGates, FortiManager, clients). You can minimize them to a bar and open
  them again with their content intact, while you use the WAN sliders.
- A console on every LAN client from the Access page, with automatic login.
- A Proxmox serial console on the FortiGates and the FortiManager as well (next to SSH), also when the network to
  the device is broken.
- No GUI/desktop on the clients (not needed: a terminal is enough).

## Non-goals
- Sessions do not survive a page reload.
- No consoles on the wan-emu or Traffic-Gen.
- No graphical VNC console (paste does not work there).

## Constraint found
Proxmox 9.1.4 refuses the VM serial console (qemu termproxy) to an API token: `value 'root@pam!hermes' does not
look like a valid user name` (tested on VM 117 with serial0; the same token does work for an LXC console, and
`qm terminal 117` on the host shows `site1-3 login:`). VM consoles therefore need a normal Proxmox user with a
password (Sander's choice: SSH stays, plus this user for the VM consoles).

## Approaches (choice: C)
- A. NICs from the tool CT itself into the spoke LANs: only works if the tool runs as a CT on the same node/bridge.
  The management network and the lab come together in one container.
- B. LAN NICs on the wan-emu + an SSH jump host to the clients: works, but needs extra interfaces and a firewall rule
  on the wan-emu.
- **C. Proxmox console through the API** (`termproxy` + `vncwebsocket`), with the API token the tool already uses.
  Tested on PVE01: token `root@pam!…` opens the console of CT 118 and gets the login prompt. No network changes,
  works wherever the tool reaches Proxmox. Sander's choice: C if it works, otherwise B. It works.

## Design
**Client console (backend)**
- Websocket `/api/console/{name}`: the tool looks up the CT by its Proxmox name (with prefix, tag fgt-sdwan) and
  asks for `POST /nodes/{node}/lxc/{vmid}/termproxy`. It then connects to `vncwebsocket` (token header + ticket) and
  passes data between xterm.js and Proxmox (termproxy protocol: `0:len:data`, resize `1:cols:rows:`, keepalive `2`).
- Automatic login: on `login:` the tool sends `root`, on `Password:` the client password. The tool keeps the client
  password at deployment, next to the FortiGate/FMG logins (0600 DB, never in the export). Without a kept password
  the user logs in by hand.
- Clear errors: CT not found, token without `VM.Console` ("give the Proxmox token VM.Console"), CT stopped.

**VM console (FortiGates, FortiManager)**
- Per Proxmox server, optional: "Console user" (e.g. `fgtsdwan@pve`, advice: its own user with only VM.Console /
  role PVEVMUser) + password. The tool logs in with it (`POST /access/ticket`, no 2FA), asks for
  `POST /nodes/{node}/qemu/{vmid}/termproxy` with that ticket + CSRF and connects to `vncwebsocket` with the
  `PVEAuthCookie`. Same protocol and proxy as the client console. LXC consoles keep using the token.
- **The password is encrypted**: Fernet (AES-128-CBC + HMAC, library `cryptography`) in the DB; the key in its own
  file `console.key` (0600) in the tool's data folder, created on first use. Never in an export or the API
  response (only "set: yes/no"). A leaked DB/export holds no usable password; root on the tool host can still
  decrypt it (key and DB on the same machine) - stated in the UI hint.
- New FortiGate and FortiManager VMs get `serial0: socket` and `vga: serial0` (also a text console with paste in the
  Proxmox GUI). Existing VMs: the console says "no serial port: deploy again, or add serial0 + Display Serial
  terminal 0 and restart the VM".
- FortiManager: whether the FMG VM talks on serial0 gets tested first on VM 111 (one restart, Sander agreed). If
  it does not, FMG keeps only SSH and does not get serial0/vga.
- Automatic login: FortiGate/FMG `login:` -> admin + the kept password, as with SSH.
- Errors: no console user set ("add a console user to the Proxmox server for VM consoles"), wrong password,
  user without VM.Console.

**Access page**
- Remote access gets a "LAN clients" row per client: name, IP, spoke, **Console** button, **Copy password**.
- FortiManager and FortiGates get a **Console** button next to GUI/SSH.

**Several sessions (frontend)**
- A terminal panel with a tab per open session (name + close button). A second click on the same device opens
  that session's tab, not a new one.
- **Minimize** collapses the panel into a bar at the bottom of the page, with a chip per session. A click opens it
  again; the xterm keeps its buffer, and the websocket stays open while minimized.
- Sessions stay open when you switch pages within the tool.

## Tests
- Fake Proxmox: termproxy + vncwebsocket (auth line, data, resize); backend proxy, automatic login, errors (no CT,
  403 without VM.Console).
- Fake Proxmox: /access/ticket + qemu termproxy with cookie; encryption round trip, key file 0600, password never
  in the export or API.
- Static UI test: tab bar, minimize bar, Console button per client and per FortiGate/FMG, console user field.

## Left to the builder
- Exact look of the tab/minimize bar (matching the existing style).
- Keepalive interval toward Proxmox (≈30 s).
