# Demo Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Dashboard tab that draws the running deployment (hubs on top, ISP clouds, spokes below) with clickable devices (connect) and clickable WAN links (latency/jitter/loss/bandwidth/down), plus presets and a full-screen mode for demos.

**Architecture:** A new `static/dashboard.js` holds two pure functions (`dashModel` → nodes/edges with positions, `dashSvg` → SVG string) and the tab's wiring; it reuses app.js globals (`$`, `esc`, `WAN_F`, `copyText`, `post`, `json`, `sessOpen`). The WAN apply/preset/reset calls and the access click handler in app.js are made reusable instead of copied. No backend changes.

**Tech Stack:** vanilla JS + inline SVG, CSS theme tokens in `static/style.css`; tests in Python (`tests/test_*.py`, run as scripts) with optional node for rendering (as `tests/test_ui_static.py` does).

**Spec:** `docs/superpowers/specs/2026-11-27-demo-dashboard-design.md`

## Global Constraints

- Data only from `GET /api/wanemu/links`, `GET /api/access`, `GET /api/traffic/status`; writes only via `PUT /api/wanemu/links/{id}`, `POST /api/wanemu/preset/{name}`, `POST /api/wanemu/reset`.
- Hubs = FortiGates with a `seg: "hub"` link; spokes = with `a`/`b` links; order as the agent returns them. ISP-HUB ↔ hubs, ISP-A ↔ spoke wan1, ISP-B ↔ spoke wan2.
- Slider fields, units and limits: `WAN_F` in app.js (shared, not copied).
- Line states: neutral (all 0, up) = normal; changed = amber with a label such as `80 ms · 2 %`; down = red, dashed, label `down`.
- Per-link neutral = `PUT {delay_ms:0, jitter_ms:0, loss_pct:0, rate_kbit:0, down:false}`, no confirm; "All neutral" keeps its `confirm()`.
- Traffic-Gen only when `/api/traffic/status` says `deployed: true`; wan-emu when `/api/wanemu/links` answers.
- FortiGate links ↔ access entries matched by name; links without access entry: device drawn, panel says connection data is missing.
- Refresh every 10 s only while the Dashboard tab is open and nothing is being dragged; own error area `#dash-err`.
- Full screen = `document.documentElement.requestFullscreen()` with body class `dash-full` hiding header/nav and other chrome; `#sess_panel` must still show over it.
- SVG with `viewBox`, `width:100%`; up to 8 spokes fit; label font never below 11 px on screen (scale with a minimum).
- Side panel: next to the picture ≥ 1100 px, slide-over below that, under the picture < 760 px.
- No new dependencies, no external images; light and dark theme via existing tokens.
- Icons drawn by us: FortiGate = red brick wall, white circle, flame (style of the user's example), not a vendor file.

## Review Focus

1. Agent unreachable / nothing deployed (links call fails or returns `[]`) → message in `#dash-err`, no empty or broken SVG. (Task 2 test.)
2. A spoke with only wan1 (no ISP-B link) → one line to ISP-A, no dangling ISP-B line. (Task 1 test.)
3. Device names with characters that need escaping (`<`, `"`) → escaped in SVG text and attributes. (Task 1 test.)
4. Many spokes (8) → all nodes inside the viewBox, no two spoke nodes overlap. (Task 1 test.)
5. A slider dragged while the 10 s refresh fires → the refresh is skipped, the dragged value is not reset. (Task 3 test: the timer checks a `dirty` marker.)

---

### Task 1: Layout model and SVG renderer

**Files:**
- Create: `static/dashboard.js` (pure part)
- Modify: `static/index.html` (load `dashboard.js` after `app.js`)
- Test: `tests/test_dashboard.py` (node-optional, like `test_ui_static.py`)

**Interfaces:**
- Produces `dashModel(links, access, traffic) -> {w, h, nodes: [{id, kind: "fgt"|"fmg"|"wanemu"|"cloud"|"client"|"tgen", role?: "hub"|"spoke", name, x, y, access?: object|null}], edges: [{id, from, to, link?: linkObject, kind: "wan"|"lan"|"mgmt", state: "neutral"|"changed"|"down", label: string}]}`. `links` = `/api/wanemu/links` `.links`; `access` = `/api/access` body (or `null`); `traffic` = `/api/traffic/status` body (or `null`). Cloud node ids: `isp-hub`, `isp-a`, `isp-b` (a cloud with no links is left out). WAN edge id = link id.
- Produces `dashLabel(state) -> string`: `""` for neutral, `"down"` for down, else the non-zero parts joined by `" · "` in WAN_F order with units `ms`, `ms`, `%`, `kbit/s` (e.g. `"80 ms · 2 %"`; jitter shown as `±5 ms`).
- Produces `dashSvg(model) -> string`: `<svg viewBox="0 0 w h" …>`; each device `<g class="dn" data-node="<id>">`, each WAN edge `<g class="de st-<state>" data-edge="<link id>">` with an invisible wide hit line; FortiGate icon as `<symbol id="ic-fgt">` used via `<use>`.

- [ ] **Step 1: Write the failing test** `tests/test_dashboard.py`: skip with a message when `node` is missing; otherwise load `static/app.js`-free `dashboard.js` in node (stub `window`/`document` minimal, export the three functions) with fixtures:
  - 2 hubs (seg hub), 3 spokes; site1-2 only `a` link. Assert: hubs' `y` < cloud `y` < spokes' `y`; site1-2 has exactly one WAN edge (to `isp-a`); total WAN edges = 2 + 3 + 2 = 7.
  - `dashLabel({delay_ms:80,jitter_ms:0,loss_pct:2,rate_kbit:0,down:false}) == "80 ms · 2 %"`; down → `"down"`; all zero → `""`.
  - 8 spokes: every node `0 <= x <= w`, `0 <= y <= h`; spoke x-distance ≥ 100 (viewBox units).
  - name `a<b"c` appears escaped (`a&lt;b&quot;c`) in `dashSvg` output, never raw.
  - `traffic = {deployed:false}` → no `tgen` node; `{deployed:true,url:"https://x:8443"}` → one.
  - `access = null` → FortiGates still drawn with `access: null`.
  - `access.clients = [{name:"client-site1-1", spoke:"site1-1", ip:"10.0.1.10"}]` → a `client` node below site1-1 (same x ± 1, larger y) with a `lan` edge to it; `access.fmgs` → one `fmg` node per FortiManager, top right.
- [ ] **Step 2: Run** `python -W ignore tests/test_dashboard.py` — expected FAIL (file/functions missing).
- [ ] **Step 3: Implement** the three functions in `static/dashboard.js` (escape via a local `escAttr` so the file is testable without app.js; reuse `esc` from app.js when present). Layout: rows at fixed fractions of `h` (hubs 0.14, clouds 0.42, spokes 0.70, clients 0.90), FMG + wan-emu top right; spokes spread evenly, `w = max(960, 160 * spokes + 200)`.
- [ ] **Step 4: Run** the test — expected PASS; run the whole suite `bash …/alltests.sh` — expected `fail=0`.
- [ ] **Step 5: Commit** `Dashboard: layout model and SVG renderer (hubs on top, ISP clouds, spokes below; FortiGate icon)`.

### Task 2: Dashboard tab, panels and actions

**Files:**
- Modify: `static/index.html` (nav link `#/dashboard` with `data-nav="dashboard"` after Access & WAN control; `<section id="v_dashboard" hidden>` with `#dash-err`, `#dash-presets`, `#dash-pic`, `#dash-panel`, button `#dash-full`)
- Modify: `static/app.js` (`route()`: `"dashboard"` in `VIEWS`, its own `DASH_TIMER`; make `wanApply/wanPreset/reset` usable with a target error function; extract the `#wanemu-fgts` click handler into `accessClick(e, errFn)` bound to both `#wanemu-fgts` and `#dash-panel`)
- Modify: `static/dashboard.js` (wiring: `dashLoad()`, panel rendering, clicks)
- Modify: `static/style.css` (`.dash-*`, line states, full screen, responsive panel)
- Test: `tests/test_ui_static.py` (static asserts), `tests/test_dashboard.py` (panel HTML)

**Interfaces:**
- Consumes Task 1's `dashModel`, `dashSvg`, `dashLabel`.
- Produces `dashLoad(): Promise<void>` (fetches the three endpoints, renders, keeps the open panel), `dashDevicePanel(node) -> string`, `dashLinkPanel(edge) -> string`, `accessClick(e, errFn)`, `wanPut(id, values, errFn) -> Promise<boolean>`, `wanPresetRun(name, errFn)`, `wanResetAll(errFn)`.

- [ ] **Step 1: Write failing tests**:
  - `test_ui_static.py`: `data-nav="dashboard"` in html; `"dashboard"` in route's VIEWS; `<script src="dashboard.js">` after app.js; `#wanemu-fgts` and `#dash-panel` both use `accessClick`; `.dash-full` hides the header/nav in css; `#sess_panel` z-index above the dashboard in full screen.
  - `test_dashboard.py`: `dashDevicePanel` for a FortiGate with access has `data-ssh`, `data-console`, `data-copy="<ip>"`, a GUI link; without access says `connection data` missing; `dashLinkPanel` renders one range input per `WAN_F` field with the link's values, a `down` checkbox and a `data-dneutral` button.
- [ ] **Step 2: Run** both — expected FAIL.
- [ ] **Step 3: Implement**: section + nav; route wiring; refactor in app.js (behaviour of Access & WAN control unchanged); panels; slider `change` → `wanPut`, per-link neutral → `wanPut` zeros (no confirm), presets → `wanPresetRun`, "All neutral" → confirm + `wanResetAll`; after each write `dashLoad()`; errors in `#dash-err`; agent error / no links → message (same texts as Access & WAN control), picture hidden.
- [ ] **Step 4: Run** tests and the whole suite — expected PASS, `fail=0`.
- [ ] **Step 5: Commit** `Dashboard tab: connect per device, WAN link controls, presets`.

### Task 3: Refresh, full screen, screen sizes — and a browser check

**Files:**
- Modify: `static/app.js` (route: `DASH_TIMER = setInterval(…, 10000)` that skips when `#v_dashboard .dirty` exists), `static/dashboard.js` (mark the panel `dirty` on `input`, clear after apply; `#dash-full` toggles fullscreen + `body.dash-full`; `fullscreenchange` removes the class on Esc)
- Modify: `static/style.css` (panel ≥1100 px beside, <1100 slide-over, <760 below; label min size)
- Test: `tests/test_ui_static.py`

- [ ] **Step 1: Failing tests** in `test_ui_static.py`: the timer callback checks `#v_dashboard .dirty`; `requestFullscreen` and `fullscreenchange` in dashboard.js; media queries `1100px` and `760px` for `.dash-panel`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** tests + suite — PASS. Then a local browser check with a harness (fake agent links, access and traffic data, as earlier harnesses in the scratchpad): picture at 1920×1080, 1280×800 and 375×812; click a FortiGate (panel, SSH opens the session panel), click a link (slider applies, label changes colour), preset and All neutral, full screen shows only the dashboard and the session panel still opens. Screenshots to the user.
- [ ] **Step 5: Commit** `Dashboard: refresh while open, full screen for demos, layout per screen size`, push, open the PR.
