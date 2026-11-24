# Demo SD-WAN rules on the spokes

## Why
The Traffic-Gen produces recognisable traffic (Office 365, Teams, streaming, SaaS, updates ...). With SD-WAN rules per
app group the demo shows SD-WAN steering live, and with the WAN sliders (latency/loss on wan2) traffic visibly moves.

## Decisions (Sander)
- Always on: part of the SD-WAN template for the spokes (no checkbox).
- Office 365 and Teams go over wan2 by default.

## Rules (spokes with two WANs, before the existing Local-Breakout, id 10)
| id | name | match (FortiOS ISDB internet services) | mode |
|----|------|----------------------------------------|------|
| 2 | O365 | Microsoft Office 365 (outlook, login, sharepoint, onedrive) | priority: wan2, then wan1 |
| 3 | Collab | Teams, Zoom, Webex, Google Meet, Slack | SLA (Internet check: latency/jitter/loss); wan2 preferred, wan1 when wan2 leaves the SLA |
| 4 | Streaming-Social | Netflix, YouTube, Spotify, Disney+, Prime Video, Twitch, Facebook, Instagram, WhatsApp, LinkedIn, X, TikTok, Reddit | wan1 only (priority wan1, no wan2) |
| 5 | Updates-Downloads | Windows Update, Apple Update, speedtests | load-balance over wan1 + wan2 |
| 6 | SaaS | Salesforce, Google Workspace, Dropbox, Box, GitHub, AWS, Adobe, ServiceNow | priority on lowest latency (link-cost-factor latency) |
| 10 | Local-Breakout | the rest | unchanged |

- Spokes with one WAN (e.g. without wan2): no demo rules (nothing to choose between).
- Hubs: unchanged.
- The Corporate rule (id 1, overlay) stays first.

## How
- Added to the spoke SD-WAN template that the tool builds from the Orchestrator (`r["sdwan"]["service"]`), after
  converting the Orchestrator CLI, for roles with two underlay members.
- SLA for Collab: its own SLA entry on the "Internet" health check (latency 150 ms, jitter 30 ms, loss 2 %), a
  builder's call, tunable later.
- The ISDB names are checked against the FortiManager at deployment (internet-service-name in the ADOM); a rule
  with unknown names drops those names, and a rule without any known name is skipped, with a warning in the log.
- The Traffic-Gen apps that hit an ISDB service are the yardstick; groups without a good ISDB match (web, sdwan,
  webfilter, malware, ioc) fall to Local-Breakout.

## Tests
- Spoke role with wan2: services 1, 2-6, 10 in that order, members per rule correct; spoke without wan2 and hubs:
  no demo rules.
- Name check: unknown names dropped, rule without names skipped, warning.

## Left to the builder
- Exact ISDB names per FortiOS/FMG version (verify against the FMG of the lab).
- Health-check SLA values.
