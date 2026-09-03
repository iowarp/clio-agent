---
id: v2ex-avenues
title: V2EX Avenues Testing Agent
display_name: V2EX Avenues Testing Agent
version: 0.1.0
description: >-
  Extended single-expert probe pack proving the Agent Blueprint MCP path
  reaches the FULL synthetic v2 exerciser tool matrix
  (tests/test_tools/mcp_exerciser.py) through a real session/turn -- the
  C1-S6 EXPANDED avenue leg (leg_c2_v2_avenues.py). Sibling of
  agents/v2ex-testing/ (which exposes only task_echo + guarded_input for
  leg C); this pack exposes every tool leg C2's live avenues need
  (task-modes, waits/cancel, pagination/readiness) so ONE session/turn can
  drive several avenues without re-installing a blueprint per avenue.
root_expert: main
blueprint:
  format: agent-blueprint-v1
# PLACEHOLDER: the exerciser is a repo-local test module launched by an
# absolute interpreter + script path, which must never be a hardcoded drive
# path committed to source. The leg overwrites this value at run time via
# `_common.py::materialize_testing_pack` before installing the pack -- see
# that function's docstring (mirrors agents/v2ex-testing/AGENT.md exactly).
# This literal string is never actually launched.
mcp_servers:
  v2ex: PLACEHOLDER_MATERIALIZED_AT_RUNTIME
experts:
  - experts/main.md
---

# V2EX Avenues Testing Agent

Live-verification-only probe pack (`scripts/live_verification/`). Declares
the synthetic v2 exerciser MCP server and a single `main` expert whose
`tools:` frontmatter names every namespaced tool the exerciser serves
(`v2ex_task_echo`, `v2ex_task_optional_echo`, `v2ex_plain_echo`,
`v2ex_forbidden_echo`, `v2ex_guarded_input`, `v2ex_plain_guarded_input`,
`v2ex_url_guarded_input` (C1-S4, #1284), `v2ex_staller`, `v2ex_plain_staller`,
`v2ex_silent_sleeper`, `v2ex_ui_echo`) -- the same Agent Blueprint mechanism
`agents/v2ex-testing/` and `agents/web-testing/` already prove works (#1301).
Rides the SAME materialization mechanics
(`_common.py::materialize_testing_pack` patches `mcp_servers.v2ex` to the
run's resolved `<python> <EXERCISER_PATH>` command at run time -- this
committed frontmatter value is a placeholder never actually launched).
