# FortiFlex-licenties automatisch — ontwerp

## Doel
De tool haalt per FortiManager/FortiGate zelf een FortiFlex-licentie (serienummer + token) op via de FortiFlex API v2,
zodat serienummers en tokens niet meer met de hand ingevuld hoeven te worden.

## API (uit de documentatie in flex/)
- Login: `POST https://customerapiauth.fortinet.com/api/v1/oauth/token/` met
  `{"username", "password", "client_id": "flexvm", "grant_type": "password"}` → `access_token` (Bearer).
- Basis: `https://support.fortinet.com/ES/api/fortiflex/v2`. Alle calls zijn `POST` met JSON; antwoord heeft
  `status` (0 = goed), `message`, `error`.
- Gebruikt: `/programs/list`, `/configs/list {programSerialNumber}`, `/entitlements/list {configId}`,
  `/entitlements/vm/create {configId, count: 1, description, endDate: null}`, `/entitlements/vm/token {serialNumber}`,
  `/entitlements/reactivate {serialNumber, followProgram: true}`, `/entitlements/update {serialNumber, configId,
  description}`, `/entitlements/stop {serialNumber}`.
- Limiet: 100 calls/minuut, 3000/uur.

## Pagina "FortiFlex"
- API-gebruiker + wachtwoord, opgeslagen in de tool-db (0600); het wachtwoord wordt nooit teruggegeven.
- "Testen": inloggen en programma's tonen. Keuze programma, configuratie voor de FortiManager (producttype
  FortiManager VM) en voor de FortiGates (FortiGate VM), met hun parameters.
- Schakelaar "FortiFlex gebruiken bij de uitrol" (zonder: handmatige tokens/serienummers zoals nu).
- Lijst van de licenties van de tool (omschrijving begint met `fgt-sdwan `) met status, tokenstatus, einddatum en een
  knop "Stop" (met bevestiging).

## Licentie per device (bij de uitrol)
Device = de FortiManager (alleen als hij in deze uitrol een token nodig heeft) en elke FortiGate (idem). Omschrijving:
`fgt-sdwan <ADOM> <devicenaam>`. Zoeken in de entitlements van de gekozen configuratie: eerst op het serienummer dat
al in de topologie staat, dan op de omschrijving.

| Gevonden | Actie |
|---|---|
| ACTIVE/PENDING, token NOTUSED | token gebruiken |
| ACTIVE/PENDING, token USED | nieuw token (`/vm/token`) |
| STOPPED | `reactivate` (followProgram), daarna nieuw token |
| EXPIRED of niet gevonden | nieuwe entitlement (`/vm/create`, kost punten) |

Een op serienummer gevonden licentie met een andere omschrijving krijgt de tool-omschrijving (`/update`).

- Moment: bij `POST /api/deploy`, vóór validatie en renderen (serienummers zijn daar al nodig). Het resultaat staat als
  stap **Licenties** (eerste stap) in de voortgang. Serienummers gaan in de topologie, tokens in de geheimen van de job
  (gemaskeerd, nooit opgeslagen).
- Alleen voor rollen die een token nodig hebben: FortiManager bij methode day0/ssh, FortiGates bij day0/ssh. Bij
  *Alleen model devices* / *Alleen templates* geen FortiFlex (handmatige serienummers zoals nu).
- Dry-run en de knop **Licenties controleren** (`POST /api/fortiflex/check`): alleen opzoeken; tonen per device het
  serienummer en de actie; nooit iets aanmaken, activeren of een token genereren. Bij een dry-run krijgt een device
  zonder licentie een unieke plaatshouder `FGVMNEWLIC000001`, `…002` (FortiManager `FMGVMNEWLC…`).
- Model device bestaat al met een ander serienummer: de tool zet het nieuwe serienummer (`update` op dvmdb); weigert
  FortiManager dat, dan stopt de uitrol met de opdracht het model device te verwijderen.
- Fouten van FortiFlex (login, rechten, punten op): de uitrol start niet; de melding van FortiFlex wordt getoond.
- De tool stopt nooit zelf licenties.
