# V2EX Testing Agent

Live-verification-only testing agent (`scripts/live_verification/`). Its
`v2ex-testing` plugin declares the synthetic v2 exerciser MCP server
(`tests/test_tools/mcp_exerciser.py`) as `v2ex`. The server's `mcp.json` is not
committed: `_common.py::materialize_testing_marketplace` writes it at run time,
because the launch command carries the run's own exerciser path, which must never
be a hardcoded drive path in source. Leg C drives `v2ex_task_echo` and
`v2ex_guarded_input`.

Layout: a native marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/v2ex-testing/`, `agents/v2ex_testing.py`), installed by `_common.py::install_testing_agent` as marketplace `lv-v2ex-testing` and selected as agent `lv-v2ex-testing/v2ex-testing`. See `docs/PLUGINS_AND_FLOWS.md`.
