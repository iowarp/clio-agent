# Asynchronous Task Qualification

Live-verification-only qualification agent (`scripts/live_verification/`). Use
only the owned workspace data and instructions supplied by the qualification
operator; authoritative tool telemetry and owner outcomes determine acceptance.
The agent carries `core` (task controls, `ask_user`), `workspace` and `shell`.
A leg that needs an MCP backend passes it to
`_common.py::materialize_testing_marketplace`, which writes the plugin's
`mcp.json` at run time.

Layout: a native marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/task-testing/`, `agents/task_testing.py`), installed by `_common.py::install_testing_agent` as marketplace `lv-task-testing` and selected as agent `lv-task-testing/task-testing`. See `docs/PLUGINS_AND_FLOWS.md`.
