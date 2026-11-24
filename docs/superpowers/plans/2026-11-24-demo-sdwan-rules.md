# Demo SD-WAN rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fixed demo SD-WAN rules for the Traffic-Gen apps in the SD-WAN template of spokes with two WANs.

**Architecture:** `build_plan` adds the rules to `r["sdwan"]["service"]` of spoke roles with two underlay members, with *search keywords* instead of fixed ISDB names. At deployment the templates step reads the internet-service names from the FortiManager ADOM and turns keywords into existing names; a rule without a hit is skipped with a warning.

**Tech Stack:** Python, FortiManager JSON-RPC.

**Spec:** `docs/superpowers/specs/2026-11-24-demo-sdwan-rules-design.md`

## Global Constraints
- Always on; only spoke roles with 2 underlay members; hubs and one-WAN spokes unchanged.
- Order: Corporate (1), O365 (2), Collab (3), Streaming-Social (4), Updates-Downloads (5), SaaS (6), Local-Breakout (10).
- O365 and Collab prefer wan2 (member 2), fallback wan1.
- Collab SLA on the Internet health check: latency 150 ms, jitter 30 ms, loss 2 %.

## Review Focus
- The FMG has no ISDB list (call fails / empty) -> all demo rules skipped with one warning, the deployment goes on (test in Task 2).
- A keyword matches many names (e.g. "teams") -> only names whose words contain the keyword, case-insensitive, at most 10 per keyword (Task 2).
- Spoke role mixes devices with and without wan2 -> roles are already split by signature; the rules follow the role (Task 1).
- Dry-run -> no FMG lookup, rules listed in the preview with their keywords (Task 2).

---

### Task 1: Rules in the plan
**Files:** Modify `app/fmgtemplates.py`; Test `tests/test_sdwan_rules.py` (new).

**Interfaces:** Produces `DEMO_RULES` (list of dict: id, name, keywords, mode, members order) and `demo_services(sdwan: dict) -> list[dict]` (service rows with `"_isdb_keywords": [...]` instead of `internet-service-name`); `build_plan` puts them between id 1 and id 10 for edge roles with 2 underlay members.

- [ ] Test: multi- and single-VRF, spoke with wan2 -> service ids `[1, 2, 3, 4, 5, 6, 10]`; O365 `mode == "priority"`, `priority-members == ["2", "1"]`; Collab `mode == "sla"`, `priority-members == ["2", "1"]`, an SLA on "Internet" with 150/30/2; Streaming `priority-members == ["1"]`; Updates `mode == "load-balance"`, members `["1", "2"]`; SaaS `mode == "priority"`, `link-cost-factor == "latency"`; spoke without wan2 and hubs: no ids 2-6.
- [ ] FAIL -> implement -> PASS -> commit.

### Task 2: Resolve ISDB names at deployment
**Files:** Modify `app/fmgapply.py` (before "SD-WAN templates"), `app/fmg.py`; `tests/mock_fmg.py` (answer `pm/config/adom/<a>/obj/firewall/internet-service-name` with `STATE["isdb"]`); Test `tests/test_sdwan_rules.py`.

**Interfaces:** `resolve_isdb(names: list[str], plan) -> list[str] warnings` sets `internet-service: "enable"` + `internet-service-name` per rule and removes `_isdb_keywords`; a rule without a hit is removed.

- [ ] Test with names `["Microsoft-Office365", "Microsoft-Teams", "Netflix-Netflix", "Google-YouTube"]`: O365 -> `["Microsoft-Office365"]`, Collab -> `["Microsoft-Teams"]`, Streaming has Netflix + YouTube, SaaS removed + warning "SaaS: no internet services found"; empty list -> all 2-6 removed, one warning; no `_isdb_keywords` left in the calls sent to FMG.
- [ ] FAIL -> implement -> PASS -> all tests -> commit, PR.

### Task 3: Lab check
- [ ] After merge/update and a deployment: the log shows which ISDB names were found; on site1-1 `diagnose sys sdwan service` shows rules 2-6; a Traffic-Gen run + WAN slider on ISP-B (latency 300 ms) moves Collab to wan1 (`diagnose sys sdwan service 3`).
