# Self-update + simple Deploy page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace About with an in-app update (GitHub main vs running commit) and make a Simple Deploy page the default (Advanced = today's page).

**Architecture:** Backend `app/updates.py` (version check with a cached GitHub compare, start of `ct/install.sh` through `systemd-run`) + 3 routes. Frontend: nav badge + panel; one form with a `data-mode` attribute that hides Advanced-only elements via CSS, a generic password that fills the four password fields. Model: Traffic-Gen always on, IP derived from the uplink.

**Tech Stack:** FastAPI, requests, vanilla JS/CSS.

**Spec:** `docs/superpowers/specs/2026-11-25-self-update-simple-deploy-design.md`

## Global Constraints
- GitHub: `https://api.github.com/repos/s-ruiter/fgt-sdwan-provision/compare/<own sha>...main`, cache 600 s, timeout 8 s.
- Update command: `systemd-run --unit fgt-sdwan-update --collect bash /opt/fgt-sdwan-provision/ct/install.sh` (APP_DIR from the running checkout).
- Update refused (409) while a deploy job runs or the cleanup lock is held.
- Simple = always 2 hubs + 3 spokes (defaults), Multi-VRF checkbox (DIA on for both WANs), one strong password, Traffic-Gen always on (IP = uplink IP − 1 when empty).
- Simple/Advanced remembered in localStorage key `fgt-sdwan-mode`.

## Review Focus
- GitHub unreachable / rate-limited -> no badge, panel says "could not check", no error page (Task 1).
- Running checkout is not a git repo (dev run) -> version "unknown", no update offered (Task 1).
- Saved setting with 4 spokes loaded in Simple -> values kept, notice "custom topology: shown in Advanced" (Task 3).
- Generic password changed after a separate Advanced password -> only fields not set separately follow (Task 3).
- Uplink IP missing -> Traffic-Gen IP stays empty and validation names the uplink, not Traffic-Gen (Task 2).

---

### Task 1: Version check + update (backend)
**Files:** Create `app/updates.py`; Modify `app/main.py`; Test `tests/test_updates.py`.
**Interfaces:** `updates.current(app_dir) -> str|None`; `updates.check(app_dir, fetch=requests.get) -> {"current","latest","behind":int,"commits":[{"sha","title"}],"error":str|None}` (cached 600 s); `updates.start(app_dir, run=subprocess.run) -> None`. Routes: `GET /api/version`, `POST /api/update` (409 when a job runs or cleanup lock held; 409 when not behind), and `/api/about` stays.
- [ ] Tests: fake fetch -> behind 2 with titles; same sha -> behind 0; fetch raises -> error set, behind 0; non-git dir -> current None, behind 0; `/api/update` with a running job -> 409; ok -> captured command equals the Global Constraint command.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 2: Traffic-Gen always on, IP derived
**Files:** Modify `app/model.py` (DEFAULTS trafficgen enabled True; `merged` fills ip = uplink IP − 1 /prefix when empty and uplink valid), `app/main.py` (no change of meaning for scopes fmg/tpl/dev); Tests `tests/test_trafficgen.py`, `tests/test_wan_links.py`.
- [ ] Tests: merged with uplink 10.10.106.250/24 -> trafficgen enabled, ip `10.10.106.249/24`; explicit ip kept; no uplink -> ip "" and no Traffic-Gen validation error besides the uplink one.
- [ ] FAIL -> implement -> PASS (fix tests that assumed enabled False) -> commit.

### Task 3: Frontend: About out, update badge/panel, Simple/Advanced, generic password
**Files:** Modify `static/index.html`, `static/app.js`, `static/style.css`; Test `tests/test_ui_static.py`.
**Interfaces:** ids `upd_badge`, `upd_panel`, `upd_go`, `mode_simple`, `mode_adv`, `pw_all`; class `adv` on Advanced-only cards/fields; `body[data-mode="simple"] .adv { display:none }`.
- [ ] Static test: no `data-nav="about"` / `v_about`; `/api/version` and `/api/update` in app.js; mode switch ids; `pw_all` with eye toggle and strength hint; `tg_on` gone; `.adv` rule in CSS; sec_devices, port table, wan-segs, VM sizes, LAN client password field carry `adv`; Multi-VRF checkbox NOT `adv`; custom-topology notice text present.
- [ ] FAIL -> implement (generic password copies into fmg_pass/fgt_newpw/client_pw/tg_pw unless that field was edited by hand in Advanced; collect() unchanged) -> PASS -> browser check (Simple, Advanced, update panel against a local uvicorn with a faked version) -> commit.

### Task 4: Final review, PR
- [ ] All tests; whole-branch review; fixes; PR.
