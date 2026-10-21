# Web Testing Agent

Live-verification-only testing agent (`scripts/live_verification/`). The agent
carries `core`, the default marketplace's `web` plugin (the `clio-kit mcp-server
web` server, so `web_fetch`, `web_search` and `web_fetch_events` reach it) and this
marketplace's `web-testing` plugin, which holds only its prompt fragment.

The prompt asks for concise human results. Authoritative structured tool results
remain the qualification source; raw JSON stays in the tool's expandable details.

Layout: a native marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/web-testing/`, `agents/web_testing.py`), installed by `_common.py::install_testing_agent` as marketplace `lv-web-testing` and selected as agent `lv-web-testing/web-testing`. See `docs/PLUGINS_AND_FLOWS.md`.
