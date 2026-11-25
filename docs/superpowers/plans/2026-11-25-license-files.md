# Licence files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy with FortiCare licence files (per device, serial from the file name) as an alternative to FortiFlex, chosen on a renamed Licenses page.

**Architecture:** `app/licfiles.py` stores files + metadata and the mode; routes in `app/main.py`; the deploy flow in files mode fills serials from the files and passes the file bytes to `build_specs`, which puts them on the Day-0 config drive (`openstack/content/0000` + `meta_data.json` files entry). The Licenses page switches between the FortiFlex cards and a per-device upload table.

**Tech Stack:** FastAPI (UploadFile), pycdlib, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-25-license-files-design.md`

## Global Constraints
- Mode setting key `licensing` in the tool DB: `fortiflex` (default) | `files`. FortiFlex settings are never touched by a mode change.
- Storage `<DATA_DIR>/licenses/` (0700), `<device>.lic` (0600), metadata `licenses.json` `{device: {"serial","file","size","uploaded"}}`.
- Serial from the file name: FortiGate `^FGVM[A-Z0-9]{8,16}$`, FortiManager `^FMG-?VM[A-Z0-9]{6,16}$` (stem, case-insensitive, stored upper).
- Config drive: `/openstack/content/0000` = file bytes, `/openstack/latest/meta_data.json` = `{"files": [{"path": "license", "content_path": "/content/0000"}]}`.
- Files are never removed by a deployment.
- Licence files only for Day-0 (VMs created by the tool); SSH/ZTP stays FortiFlex-only.

## Review Focus
- Files mode, a device without a file -> one clear error per device before anything is created (Task 3).
- File for the FortiManager uploaded on a FortiGate row (or the reverse) -> refused with the kind in the message (Task 1).
- Same serial uploaded for two devices -> refused (Task 1).
- Device renamed in the form after uploading -> the row shows "no file" for the new name; the old file stays listed as unused (Task 4).
- Mode files but FortiFlex was enabled -> no FortiFlex calls at all during deploy/check (Task 3).

---

### Task 1: Storage, serial parsing, mode
**Files:** Create `app/licfiles.py`; Test `tests/test_licfiles.py`.
**Interfaces:** `serial_from_name(filename) -> (kind: "fgt"|"fmg", serial) | raises ValueError`; `save(data_dir, device, kind, filename, content: bytes) -> meta`; `listing(data_dir) -> {device: meta}`; `read(data_dir, device) -> bytes|None`; `delete(data_dir, device) -> bool`; `get_mode(conn) -> str`; `set_mode(conn, mode)`.
- [ ] Tests: `FGVM08TM24001234.lic` -> ("fgt","FGVM08TM24001234"); `FMG-VMTM24005678.lic` -> ("fmg",…); `foo.lic` -> ValueError; save fgt file on kind fmg -> ValueError "FortiGate"; duplicate serial on another device -> ValueError; file mode 0600, dir 0700; listing/read/delete; mode default fortiflex, set files, FortiFlex secret rows unchanged.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 2: Config drive with a licence file
**Files:** Modify `app/cloudinit.py` (`build_config_drive(user_data, dest, license=None)`), `app/provision_vms.py` (`build_specs(..., licenses=None)` puts `spec["license"]`; deploy_vms passes it); Test `tests/test_day0_units.py` or `tests/test_licfiles.py`.
- [ ] Tests: drive built with license -> `read` of `/openstack/content/0000` == bytes and `meta_data.json` has the files entry; without license -> no meta_data.json (unchanged); build_specs with licenses -> FGT user_data has no LICENSE-TOKEN part, FMG user_data has no `execute vm-license`.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 3: Deploy flow + routes
**Files:** Modify `app/main.py` (`_licenses`: files mode -> serials from files, no FortiFlex; `_check_bootstrap`: in files mode a missing file is the error instead of the token; boot gets `licenses: {device: bytes}`; `/api/fortiflex/check` in files mode returns per device file/serial or "missing"; routes `GET /api/licenses`, `PUT /api/licenses/mode`, `POST /api/licenses/{device}` (UploadFile, kind from the device: fmg name -> fmg, else fgt), `DELETE /api/licenses/{device}`), `app/fmg.py` (`_pve_vms` passes `boot["licenses"]` to build_specs); Test `tests/test_licfiles.py`.
- [ ] Tests (TestClient + fake pieces): mode switch keeps `/api/fortiflex` settings; upload/list/delete via API; deploy request (dry/validation) in files mode with all files -> serials in topology, no FortiFlex client created (monkeypatch `_flex_client` to raise); missing file for site1-2 -> 422 "site1-2: no licence file"; files still on disk after a (dry) deploy.
- [ ] FAIL -> implement -> PASS (all tests) -> commit.

### Task 4: UI: Licenses page
**Files:** Modify `static/index.html`, `static/app.js`; Test `tests/test_ui_static.py`.
- [ ] Static test: nav text "Licenses" (`data-nav="fortiflex"` kept as route id), `lic_mode_flex` / `lic_mode_files` radios, `lic_files` table, `/api/licenses` in app.js, FortiFlex cards wrapped in `ff-only`, deploy page FortiFlex token fields carry `flex-only` (hidden in files mode via `body[data-lic="files"] .flex-only`), unused files listed.
- [ ] FAIL -> implement -> PASS -> browser check -> commit.

### Task 5: Final review, PR (lab check with a real licence file after merge)
