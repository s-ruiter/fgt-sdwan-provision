# Demo dashboard — design

## Intent and why

Sander gives SD-WAN demos with the lab this tool deploys. During a demo he wants one screen with a clear network
picture of the running deployment: FortiGates, FortiManager, the ISP clouds and the WAN links between them. A click on
a device connects to it (GUI, SSH, console); a click on a WAN link changes it (latency, jitter, loss, bandwidth,
up/down). The audience sees the topology and what is being done to it.

The topology drawing on the Deploy page stays as it is: it shows what *will* be deployed. The dashboard shows what
*is* deployed and is the demo's control surface.

## Goals

- A new tab **Dashboard** in the tool (vanilla JS + inline SVG, the tool's existing style and themes).
- Layout: the hubs at the top, the spokes below them, ISP clouds in between (ISP-HUB for the hubs; ISP-A for spoke
  wan1, ISP-B for spoke wan2), every WAN link drawn as a line device ↔ cloud.
- FortiGates drawn with a firewall icon in the style Sander showed: a red brick wall with a white circle and a flame
  (drawn by us as SVG; not a copy of a vendor file). FortiManager, wan-emu, LAN clients and Traffic-Gen get their own
  simple icons.
- Click a device → a side panel with its connection options, the same as on Access & WAN control: GUI (new tab),
  SSH (the tool's terminal panel), Console (Proxmox console), Copy password, click-to-copy IP.
- Click a link line → a side panel with that link's sliders (latency, jitter, loss, bandwidth) and "link down", applied
  when a slider is released (as on Access & WAN control), plus "neutral" for that link: a `PUT` with delay, jitter,
  loss and rate 0 (rate 0 = unlimited) and down false — the same values "All neutral" sets. *(Claude's call: there is
  no per-link reset endpoint.)*
- The presets (ISP-A down, ISP-B down, hub 1 down, …) and "All neutral" as buttons on the dashboard.
- Each line shows what is set on it: neutral = normal line; changed = a label on the line (e.g. "80 ms · 2 %"), amber;
  down = red, dashed, with "down".
- A **Full screen** button for the projector: browser fullscreen of the whole page with the tool's menu and other
  chrome hidden, so only the dashboard shows *and* the SSH/console session panel (`#sess_panel`, outside the
  dashboard) still opens over it. *(Claude's call: fullscreening only the dashboard element would hide that panel.)*
- Works on any screen: see Screen sizes.

## Non-goals

- No live data from the FortiGates (no measured latency, SLA state, chosen link per SD-WAN rule). Only what is set
  on the WAN emulator is shown. (Sander's choice.)
- No editing of the topology, no deployment actions on the dashboard.
- Access & WAN control is not changed; the dashboard is an extra view on the same functions.

## Anti-goals

- A picture that does not match the running deployment (wrong devices, missing links).
- Controls that behave differently from Access & WAN control (other units, other limits, values applied differently).
- Anything that looks like a copy of a specific lab product or vendor artwork.

## Constraints

- Data comes only from existing endpoints:
  - `GET /api/wanemu/links` → `{addr, links: [{id, name, seg: "hub"|"a"|"b", ip, state: {delay_ms, jitter_ms,
    loss_pct, rate_kbit, down}}], presets: {name: label}}`. `name` = the FortiGate; `seg` tells hub (ISP-HUB) or
    spoke (ISP-A = wan1, ISP-B = wan2).
  - `PUT /api/wanemu/links/{id}`, `POST /api/wanemu/preset/{name}[?link=]`, `POST /api/wanemu/reset`.
  - `GET /api/access` → `fmgs` (with `key`), `fgts` (name, ip, gui, ssh, password), `clients` (name, spoke, ip).
  - SSH / console / copy password: the existing global session panel (`#sess_panel`) and its handlers
    (`data-ssh`, `data-console`, `data-cpw`, `data-copy`).
- Hubs = FortiGates with a `seg: "hub"` link, spokes = with `a`/`b` links; order as the agent returns them.
- A FortiGate's links (`links[].name`) and its access entry (`fgts[].name`) are matched by name (both come from the
  topology's device names). A FortiGate with links but no access entry is drawn, its panel says the connection data
  is missing (link panel works). *(Claude's call.)*
- Traffic-Gen is drawn only when `GET /api/traffic/status` says `deployed: true` (its panel: a link to its GUI `url`
  and to the Traffic tab). wan-emu is drawn when `/api/wanemu/links` answers (address = `addr`, no panel).
  *(Claude's call: no new endpoint needed.)*
- Behaviour shared with Access & WAN control: the same functions for applying values, presets and reset (refactored
  to take their target area, not duplicated); the dashboard has its own error area and its own 10 s timer that runs
  only while the Dashboard tab is open. "All neutral" asks for confirmation as it does today; the per-link "neutral"
  does not. *(Claude's call.)*
- Slider limits and units are the ones of `WAN_F` in `static/app.js` (shared, not copied).
- Refresh: reload the links every 10 s while the tab is open and no slider is being dragged (as Access & WAN control
  does), so a preset fired from another browser shows up.
- No new dependencies; no external fonts or images (the icons are inline SVG).
- When the WAN agent is unreachable or nothing is deployed: a clear message instead of an empty picture (the same texts
  as Access & WAN control).

## Screen sizes

- The picture is one SVG with a `viewBox`; it scales to the width of the window and keeps its proportions. The
  layout is computed from the number of hubs and spokes (2 hubs and 3 spokes is the usual case; up to ~8 spokes must
  still fit, spokes spread evenly).
- Full screen uses the whole screen (beamer 1920×1080, laptop, 4K); the picture is centred and as large as fits.
- Labels stay readable: their size scales with the picture, with a minimum.
- Side panel: next to the picture on wide screens, over it (slide-in) on narrow screens; on a phone (< 760 px) under
  the picture. Opening the panel never covers the line or device that was clicked on a wide screen.

## Approaches

1. **New tab "Dashboard" in the tool, with a Full-screen button** — chosen. Access & WAN control stays for daily work;
   the demo is one click away and full screen hides the tool's menu. Trade-off: two places to connect / change links.
2. Turn Access & WAN control into the dashboard — one place for everything, but the new copy-friendly list would go.
3. A separate demo page without the tool's menu — cleanest look, but its own URL and the tool not at hand; full screen
   in approach 1 gives the same look.

## Left to the builder

- Exact geometry, icon drawing details, colours within the tool's theme tokens (light and dark).
- Where FortiManager, wan-emu, LAN clients and Traffic-Gen sit (proposal: FortiManager and wan-emu top right, a LAN
  client small under its spoke; Traffic-Gen next to the first spoke's client) and whether a click on them opens a
  panel (FortiManager and LAN clients: yes, they have access options; wan-emu: none).
- How a hub with more than one ISP-HUB link or a spoke with only wan1 is drawn (one line per link that exists).
- Tests in the existing style (`tests/test_ui_static.py` asserts on the static files; pure layout logic in a
  function that can be tested).
