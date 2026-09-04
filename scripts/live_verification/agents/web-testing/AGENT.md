---
id: web-testing
title: Web Testing Agent
display_name: Web Testing Agent
version: 0.1.0
description: >-
  Minimal single-expert probe pack proving the Agent Blueprint MCP path
  (AGENT.md mcp_servers -> a real turn's tool gateway) reaches a declared
  clio-kit web server. Exists to prove plumbing, not reasoning (campaign
  slice C1-S6, tracking issues 1286 and 1301).
root_expert: main
blueprint:
  format: agent-blueprint-v1
mcp_servers:
  web: clio-kit mcp-server web
experts:
  - experts/main.md
---

# Web Testing Agent

Live-verification-only probe pack (`scripts/live_verification/`). Declares the
clio-kit web MCP server and a single `main` expert whose `tools:` frontmatter
names the exact namespaced tools the declared `web` server exposes
(`web_fetch`, `web_search`, `web_fetch_events`).

This is the WORKING path leg B/C ride instead of the bare-session builtin
main: the builtin main's toolset is a hardcoded 4-tool list, so a
declared-server tool never reaches it (#1301, deferred upstream). An Agent
Blueprint's declared `mcp_servers` DOES reach the real per-turn tool gateway
(`agent.py::_build_tool_gateway` -> `_discover_pack_servers` ->
`load_mcp_servers(pack_servers=...)`), the same mechanism every real
marketplace pack (e.g. `deep-researcher`) uses.

`mcp_servers.web` above is committed as a real, portable command (`clio-kit`
resolves on PATH, no drive-specific path) so this file works installed
verbatim. The leg still routes it through
`_common.py::materialize_testing_pack` before install for consistency with
`v2ex-testing` (whose command MUST be resolved at run time) -- one mechanism,
not two.
