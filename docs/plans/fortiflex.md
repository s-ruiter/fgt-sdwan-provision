# FortiFlex Implementation Plan

> Executed inline (superpowers:executing-plans), one fresh review at the end.

**Goal:** Serienummers en FortiFlex-tokens automatisch per device bij de uitrol. **Spec:** `docs/specs/fortiflex.md`

## Global Constraints
- Wachtwoord en tokens nooit in events, logs, responses naar de browser of commits; tokens via de bestaande `Masker`.
- Omschrijving `fgt-sdwan <ADOM> <naam>`; geen automatische stop van licenties.
- Tests: losse scripts `tests/test_*.py`, nep-FortiFlex-server in `tests/fake_fortiflex.py`.

## Review Focus
1. Twee devices zouden dezelfde entitlement kunnen pakken (serienummer in topologie dubbel / omschrijving) → elke entitlement hoogstens één device.
2. FortiFlex geeft `status != 0` of HTTP 401 halverwege (na een create) → duidelijke fout, en wat al aangemaakt is staat in de melding.
3. Dry-run / "controleren" mag nooit create/token/reactivate/update aanroepen.
4. Serienummer in topologie hoort bij een entitlement in een andere configuratie → niet gebruiken, behandelen als niet gevonden.
5. Scope dev/tpl of methode none → FortiFlex niet aanroepen.

### Task 1: `app/fortiflex.py` — client + planner
- `Client(user, password, auth_url=AUTH_URL, base=BASE, timeout=30)`: `programs()`, `configs(program_sn)`,
  `entitlements(config_id)`, `create_vm(config_id, description)`, `regen_token(sn)`, `reactivate(sn)`,
  `update(sn, config_id, description)`, `stop(sn)`; `FlexError(msg)` bij status != 0 / HTTP-fout.
- `desc(adom, name) -> str`; `plan(devices: list[{name, serial, config_id}], ents_by_config: dict[int, list], adom) ->
  list[{name, config_id, serial|None, action: use|regen|reactivate|create, rename: bool}]`.
- `resolve(client, plan_rows, dry) -> dict[name, {serial, token, action}]`.
- Test: `tests/test_fortiflex.py` tegen `tests/fake_fortiflex.py` (alle tabelregels, Review Focus 1–4).

### Task 2: opslag + API
- db-secrets `fortiflex_user`, `fortiflex_password`; settings `fortiflex` JSON `{enabled, program, fmg_config, fgt_config}`.
- Routes `GET/PUT /api/fortiflex`, `POST /api/fortiflex/test`, `GET /api/fortiflex/configs`, `GET /api/fortiflex/entitlements`,
  `POST /api/fortiflex/stop/{sn}`, `POST /api/fortiflex/check`.
- Test: `tests/test_fortiflex_api.py`.

### Task 3: uitrol
- `main._licenses(topo, r) -> list[(status, msg)]` vóór `_render`: vult serials + `r.secrets` tokens; `_check_bootstrap`
  ongewijzigd (tokens zijn dan gevuld). STEPS begint met `("licenses", "Licenties", …)`; pipeline emit de regels.
- `fmg.py` devices-stap: bestaand model device met ander `sn` → `update`; weigering → FmgError met instructie.
- Test: pipeline/scope-tests + mock_fmg sn-update.

### Task 4: GUI
- Pagina `#/fortiflex` (credentials, testen, programma/configs, schakelaar, licentielijst + stop).
- Uitrolpagina: bij FortiFlex aan serienummer/token-velden alleen-lezen "via FortiFlex"; knop "Licenties controleren".
- Test: `tests/test_ui_static.py`.

### Task 5: README, review, PR, CT 100.
