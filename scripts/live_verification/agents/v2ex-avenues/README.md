# V2EX Avenues Testing Agent

Live-verification-only testing agent (`scripts/live_verification/`) for
`leg_c2_v2_avenues.py`. Its `v2ex-avenues` plugin declares the synthetic v2
exerciser MCP server as `v2ex`, so every tool the exerciser serves (task modes,
waits and cancel, pagination and readiness, URL and agent elicitation, UI echo,
list-changed notifications) reaches one agent, and one session can drive several
avenues. `mcp.json` is written at run time by
`_common.py::materialize_testing_marketplace`, as for `v2ex-testing`.

Layout: a native marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/v2ex-avenues/`, `agents/v2ex_avenues.py`), installed by `_common.py::install_testing_agent` as marketplace `lv-v2ex-avenues` and selected as agent `lv-v2ex-avenues/v2ex-avenues`. See `docs/PLUGINS_AND_FLOWS.md`.
