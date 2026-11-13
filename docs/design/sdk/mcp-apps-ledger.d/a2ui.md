# Ledger fragment: `a2ui` stream (MA20 to MA22)

Rows for [mcp-apps-ledger.md](../mcp-apps-ledger.md), in its tables. Wire shapes are in
[mcp-apps-contracts.md](../mcp-apps-contracts.md), section "A2UI component".

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |
| MA20 | `clio.mcp-app.v1` joins the clio-workspace catalog with `server`, `tool`, `arguments`, `state: [{key, path, tool?}]` (at most 16, keys and paths distinct) and `height` (160 to 2000). It is built by the catalog factory, so the pydantic model and the JSON Schema come from one declaration; the distinct-keys rule is a pydantic validator and a zod refinement, since JSON Schema cannot express it. It renders with the host's `McpAppSurface` in `placement="surface"`; nothing about the app's domain is in it | plan MA20 | clio-schemas `components.py`, `tests/test_a2ui_mcp_app.py`; gact-tui `a2ui-mcp-app-catalog.ts`, `a2ui-mcp-app.tsx` | done |
| MA20a | The component opens its view through the reopen path. `POST /v1/sessions/{sid}/mcp-apps/open {server, tool, arguments}` calls `mcp_app_calls.replay_call`, which reopening now shares, with a capture whose `save` is false: the surface already holds the call, so it is not saved in `mcp_app_calls` (a saved entry per mount would grow the session metadata on every reload), and the view is not added to the transcript. Failures use the reopen reasons and carry the call; the component shows the reason, the call and Retry | plan MA19, MA20; contracts "Saved call and reopening" | `gact/mcp_app_open.py`, `tests/test_gact/test_mcp_app_open.py` | tests only |
| MA20b | The producer checks the opening call when a surface or dashboard is produced: a `server`/`tool` that is not a view-opening tool of a connected server is the typed refusal `a2ui_mcp_app_tool_not_found`, listing the server's view tools. A channel `tool` is not checked there, because it may be one of the view's own app tools, which exist only once the view is open | skill rule 5 (check where the data is) | `a2ui_producer/_mcp_app_reference.py`, `tests/test_gact/test_a2ui_mcp_app_reference.py` | tests only |
| MA20c | Mounting shares one open per (session, surface, component, call). React's development double mount and re-renders share it; the last unmount closes the app through the live set's `releaseMcpAppSurface` (MA7) after the current task, so an immediate remount keeps the view. A mounted app joins the live set with placement `surface`, which pins its record (`PUT …/pin`, MA5/MA6) | plan MA5 to MA7 | `mcp-app-surface-opens.ts`, `mcp-app-surface-host.ts` | open |
| MA21a | View to surface: a key of the view's `ui/update-model-context` `structuredContent` that a channel names is written to the channel's path. Keys are read from `structuredContent` because the specification defines it as the structured half of the context; `content` is text for the model | spec draft `ui/update-model-context` | `mcp-app-state-bridge.ts` | tests only |
| MA21b | A channel whose path is `/selection/<key>` holds the surface's shared selection: the view's value is written as `SelectionState {field: <channel key>, values: [value] or the list, source: <component id>}` and read back as the one value or the list. Views link with charts, tables and maps through the one selection mechanism (skill rule 3) without knowing A2UI. A cleared selection calls nothing, since it carries no new value. Any other path holds the value verbatim | skill rule 3; plan rule 7 | `mcp-app-state-bridge.test.ts` | tests only |
| MA21c | Surface to view: a path change the bridge did not write, whose value the view does not already show and that repeats nothing just sent, calls the channel's tool once with `{[key]: value}`. A name in the open answer's `server_tools` goes to the app's `tools/call` route, and the view then receives `ui/notifications/tool-input` and `ui/notifications/tool-result` for it, the specification's interactive-phase sequence, so a view that renders from its tool results shows the new state. Any other name is sent to the view as an app tool. Initial path values are not pushed into a newly opened view: the opening arguments set its first state | plan MA21; spec draft "Interactive Phase" | `mcp-app-state-bridge.test.ts` | tests only |
| MA21d | No echo: the bridge records the JSON of each value it writes and ignores that value when the data model notifies it; a selection whose `source` is the component is its own; a view report equal to the value just sent for the channel is the view settling on it and is not written back | plan MA21 | `mcp-app-state-bridge.test.ts` | tests only |
| MA22 | A channel writes at most once per 100 ms, at the end of the window, with the latest value: the first report starts the window and later reports replace the pending value. A trailing window rather than a leading write keeps sixty reports in one second to at most ten writes; a lone report reaches the data model 100 ms later. Failures (a tool error, a value a selection cannot hold) are shown in words on the channel row | plan MA22 | `mcp-app-state-bridge.test.ts` | tests only |

## Facts

| ID | Fact | Evidence | Status |
| --- | --- | --- | --- |
| A2-F1 | clio-agent takes clio-schemas from PyPI at an exact pin (`pyproject.toml` `clio-schemas==0.7.0b1`, `uv.lock`). This stream bumps clio-schemas to `0.7.0b2` and develops against it with `uv pip install --python .venv/bin/python -e /home/user/wt/a2ui/clio-schemas` in the worktree's virtual environment; the stack runs `uv run --no-sync`, so the editable install holds. The pin moves when 0.7.0b2 is released | `git diff` of clio-schemas `pyproject.toml`; `.venv` | open |
| A2-F2 | The catalog copies kept in step: gact-tui `web/src/test-fixtures/a2ui/v0_9_1/catalogs/clio-workspace/v1/{catalog.json,catalog.clio.json}` and their `manifest.json` hashes, `server-snapshot.catalog-sidecars.json` (regenerated from the served sidecars, which also picks up the base's `data_source/login/ndp` event), and clio-agent `site/src/data/widget-reference.json` (`node site/scripts/generate-widget-pages.mjs`). `a2ui_producer/_data_reference.py` needs no change: the component carries no data reference | file diffs | done |
| A2-F3 | Rendering a component description declared on a non-string property was dropped by clio-schemas' catalog renderer (`Tabs.activeTab`, `Carousel.activeChild`); it is now rendered, a no-silent-drop fix the new properties needed | clio-schemas `catalog_render.py` | done |

## Qualification

| ID | Scenario | Evidence | Status |
| --- | --- | --- | --- |
| AQ27 | | | open |
| AQ28 | | | open |
| AQ29 | | | open |
