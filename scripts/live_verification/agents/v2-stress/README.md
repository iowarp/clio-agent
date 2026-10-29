# V2 Stress Testing Agent

Live-verification-only stress agent (`scripts/live_verification/`) for
`leg_bd_stress.py`. The agent carries the default marketplace's `web` plugin
(`web_fetch`, `web_search`) and this marketplace's `v2-stress` plugin, which
declares the synthetic v2 exerciser as `v2ex` (its `mcp.json` is written at run
time by `_common.py::materialize_testing_marketplace`). It proves both servers
resolve onto one agent and survive a single multi-step turn: a web search, a
task-backed PDF fetch, a v2ex task-backed call and a v2ex MRTR round. It should
pass before the more expensive deep-researcher run (`leg_d_deep_researcher.md`).

Layout: a native marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/v2-stress/`, `agents/v2_stress.py`), installed by `_common.py::install_testing_agent` as marketplace `lv-v2-stress` and selected as agent `lv-v2-stress/v2-stress`. See `docs/PLUGINS_AND_FLOWS.md`.
