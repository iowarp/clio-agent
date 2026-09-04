---
id: v2-stress
title: V2 Stress Testing Agent
display_name: V2 Stress Testing Agent
version: 0.1.0
description: >-
  Combined single-expert probe pack proving the Agent Blueprint MCP path
  reaches TWO declared servers in ONE turn: the clio-kit web MCP (real
  network fetch/search, task=required) AND the synthetic v2 exerciser
  (tests/test_tools/mcp_exerciser.py). Exists as the STRESS GATE for legs B
  and D (#1286, C1-S6 owner addendum): a single research-shaped agent flow
  that forces a web_search, a task-backed PDF web_fetch (docling conversion),
  a v2ex task-backed call, and a v2ex MRTR round, all in one session -- must
  pass before the real marketplace deep-researcher pack is run separately.
root_expert: main
blueprint:
  format: agent-blueprint-v1
# PLACEHOLDER: BOTH values below are resolved/overwritten at run time by
# _common.py::materialize_testing_pack (mirrors agents/web-testing/ and
# agents/v2ex-testing/ exactly -- ONE mechanism, not two). `web`'s committed
# value is already portable (clio-kit resolves on PATH); `v2ex`'s committed
# value is a placeholder because the exerciser is a repo-local test module
# launched by an absolute interpreter + script path, which must never be a
# hardcoded drive path committed to source.
mcp_servers:
  web: clio-kit mcp-server web
  v2ex: PLACEHOLDER_MATERIALIZED_AT_RUNTIME
experts:
  - experts/main.md
---

# V2 Stress Testing Agent

Live-verification-only probe pack (`scripts/live_verification/`). Declares
BOTH the clio-kit web MCP server and the synthetic v2 exerciser server, and a
single `main` expert whose `tools:` frontmatter names exactly six namespaced
tools across both servers (`web_fetch`, `web_search`, `v2ex_task_echo`,
`v2ex_task_optional_echo`, `v2ex_guarded_input`, `v2ex_staller`) -- within
CLAUDE.md's RULE 5 curated 5-7-tools-per-expert ceiling.

This is the SAME Agent Blueprint path `agents/web-testing/` and
`agents/v2ex-testing/` already prove works individually (#1301); this pack's
whole point is proving BOTH declared servers resolve onto ONE agent's
toolset simultaneously and both survive a SINGLE multi-step turn (the actual
shape the marketplace `deep-researcher` pack uses in production, and the
shape `leg_d_deep_researcher.md` runs separately against that real pack).
`leg_bd_stress.py` drives this pack directly; it is the cheap, deterministic
stress rehearsal that should go green BEFORE spending the much more
expensive multi-session deep-researcher run.
