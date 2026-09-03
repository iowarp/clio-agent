---
id: v2ex-testing
title: V2EX Testing Agent
display_name: V2EX Testing Agent
version: 0.1.0
description: >-
  Minimal single-expert probe pack proving the Agent Blueprint MCP path
  reaches the synthetic v2 exerciser server (tests/test_tools/mcp_exerciser.py)
  through a real session/turn. Exists to prove plumbing, not reasoning
  (campaign slice C1-S6, tracking issues 1286 and 1301).
root_expert: main
blueprint:
  format: agent-blueprint-v1
# PLACEHOLDER: the exerciser is a repo-local test module launched by an
# absolute interpreter + script path, which must never be a hardcoded drive
# path committed to source. The leg overwrites this value at run time via
# `_common.py::materialize_testing_pack` before installing the pack -- see
# that function's docstring. This literal string is never actually launched.
mcp_servers:
  v2ex: PLACEHOLDER_MATERIALIZED_AT_RUNTIME
experts:
  - experts/main.md
---

# V2EX Testing Agent

Live-verification-only probe pack (`scripts/live_verification/`). Declares
the synthetic v2 exerciser MCP server and a single `main` expert whose
`tools:` frontmatter names the exact namespaced tools this leg drives
(`v2ex_task_echo`, `v2ex_guarded_input`).

This is the WORKING path leg C rides instead of the bare-session builtin
main: the builtin main's toolset is a hardcoded 4-tool list, so a
declared-server tool never reaches it (#1301, deferred upstream). An Agent
Blueprint's declared `mcp_servers` DOES reach the real per-turn tool gateway,
the same mechanism every real marketplace pack uses.
