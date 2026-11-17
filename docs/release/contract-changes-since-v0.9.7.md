<!-- Inventory produced overnight on 2026-11-17 by a read-only helper agent from git history (f4b05a1..ba8c47f). Spot-checked (pins at v0.9.7.1, PR numbers #2305/#2354/#2358/#2364/#2365) but not verified line by line: confirm each item against the code before using it in the CHANGELOG. -->

# GACT-contract changes in f4b05a1..origin/develop (ba8c47f) vs CHANGELOG `## Unreleased`

Range: 533 non-merge commits. PR numbers: each commit is credited to the earliest "Merge pull request #N" whose second parent brings it in. 515 commits map this way. The other 18 are the SDK work, which came in through `ba8c47f Merge branch 'claude/clio-python-sdk' into develop`, and that merge names no PR. Most feature work arrived through umbrella PRs (#2358, #2364 and #2365 from `claude/parallel-agents-plan-uzpzm0`), so one PR number covers many items. Issue numbers are cited only where a commit message names one.

Legend: COVERED = already in develop's `## Unreleased`; MISSING = not there.

## Pins and section structure (task item 3)

- The premise is wrong: v0.9.7.1 did not ship clio-kit 2.12.5 or 2.12.7. At tag `v0.9.7.1`, `pyproject.toml`, `install.sh` and `install.ps1` all pin `clio-kit==2.12.3`.
  - The 2.12.5 and 2.12.7 pins landed on main after the tag (b0652f9f0, b84532cd1, 8276d353c, 42aea4d5b, d48d5e645). No tag exists after v0.9.7.1.
  - `origin/main:CHANGELOG.md` lists both pins under `## Unreleased` → `### Changed`.
  - On develop the pins came through #2307 and #2310 (2.12.5: b4285520b, 1be258353) and #2323 and #2327 (2.12.7: 3fc8e81fa, e2555f69c, 287edaf8e). develop's `pyproject.toml` has `clio-kit==2.12.7`.
  - Verdict: the pins belong in the next release, but under `### Changed`, as on main. On develop they sit, misfiled, under `### Removed` after a blank line, so they read as if they were removals. If 0.9.7.2 is cut from main first, they move into that section instead.
- develop's CHANGELOG has no `## [0.9.7.1]` section; it goes from Unreleased straight to `[0.9.7]`. The release must merge main's 0.9.7.1 section.
- The fixes that 0.9.7.1 shipped are also in this range through their develop PRs. They should not be listed again:
  - #2301 (9200deb56), shipped as #2302
  - #2289 (42767a67a), shipped as #2291
  - #2290 (0bd5943e0), shipped as #2295
  - #2296 (922773a06), shipped as #2297
  - #2287 (27a3f1925), shipped as #2288
  - #2292 (5b60f7211)
- Also on main after v0.9.7.1 and on develop: "retry every failed bundled-git source fetch" (develop 776f5e071 / #2314, main c2b161dfe). This is an installer fix, not a contract change.
- Not contract, but release-blocking: develop pins `clio-schemas==0.8.0b1` (it was 0.7.0) and builds both clio-schemas and clio-kit from git through `[tool.uv.sources]`. release-clio must switch these back to PyPI pins.

## Breaking

| Change | Commit / PR | Status |
|---|---|---|
| Python client constructor: `ClioClient(base_url, headers=…)` → `ClioClient(options=ClioOptions(...), transport=None)`. Constructing a client now attaches to a server or starts one (`launch=True` by default) and registers it as an attached client. `DEFAULT_BASE_URL` is removed from `clio_agent.client`. | 2038b2f7f (no PR) | MISSING. Unreleased "Removed" names only `routing_mode` |
| Python client: `sessions.create(routing_mode=…)` removed. It now takes `agent`, `model`, `approval_mode`, `flow`, `unattended`, `max_turns` and `max_budget_usd`. `sessions.update(routing_mode=…)` is replaced by `approval_mode`. | 2038b2f7f (no PR) | COVERED (routing_mode only) |
| `AgentRef.id` has no `"main"` default (`Field(min_length=1)`), so `agent: {}` or `{mode}` without an id is now a 422. `agent` itself is still optional. | b6a66fcd7 (no PR) | COVERED |
| `POST /v1/permissions/{id}/respond` with an action the question does not offer: the error code changed from `internal_error` to `invalid_action`. The message lists the offered answers. A question that names its own answers accepts only those. | 06932cab1, e936a066a (#2364) | MISSING |
| `metadata` on session create, `PATCH` and message marks refuses server-owned keys with `metadata_reserved`: `agent_flow`, `unattended`, `run_limits`, `unattended_trusted`, `run_limit_reached`, `unattended_approved`. | b6a66fcd7 (no PR) | COVERED |
| `clio-agent -q`: grants every permission (it used to auto-deny), answers questions with the not-available reply, and its JSON drops `selected_expert`. | 2038b2f7f, f09de643d (no PR) | COVERED |
| The interactive `clio-agent` REPL boots through the library (`ClioOptions`, `ensure_server(stop_with_last_client=True)`). A server the REPL starts now stops when its last client leaves; before, a detached server kept running. | 2038b2f7f (no PR) | MISSING |
| `serve.ensure_server` spawns `clio-agent serve`, not `clio-agent-gact`. | b6a66fcd7 (no PR) | COVERED |
| Installers no longer build the agents' clio-kit tool servers at install time ("lazy install"). The new opt-in flags `--prepare-tool-servers` (install.sh) and `-PrepareToolServers` (install.ps1) restore it for offline use. A server bound to a connector is mounted only while that connector is on (typed `connector_off`). | 4559109d3 (#2358) | MISSING |
| A fresh `ws_default` is seeded with `<data>/workspace`, not the server's launch folder, unless `paths.default_workspace` is set. An existing workspace keeps its root. Probed stdio servers start there too. | 2299dc0d6 (#2358) | MISSING |
| A turn ended by service shutdown is recorded as `interrupted` (resumed after restart), not `stopped`. A turn failing from the server's own SIGTERM reads `cancelled`, `reason: server_shutdown`. Restart-cut replies carry `server_restart_interrupted`. | f3892ede8, 9dc7d473e, cb3cae0b2, e2d24c0cc (#2365) | MISSING |

## Added

### Python library and script runs (all from the SDK merge ba8c47f, no PR)

| Change | Commit | Status |
|---|---|---|
| `clio` module (`src/clio`, re-exports `clio_agent.client`): `ClioClient`, `AsyncClioClient`, `ClioOptions`, `RunResult`, `RunSession`, `Artifact`, `TaskStarted`/`TaskSettled`, and the errors `ServerSetupError`, `ServerUnavailable`, `ServerStartFailed`, `ProviderNotConfigured`, `AgentNotAvailable`. Methods: `run`, `run_flow`, `stream_flow`, `session`. | 2038b2f7f | COVERED |
| `POST /v1/sessions` accepts `flow {id, options}`, `unattended`, `max_turns` and `max_budget_usd`. Refusals are typed: `flow_options_invalid`, `flow_not_trusted` and the other flow refusals. | b6a66fcd7 | COVERED (`flow_not_trusted` is not named) |
| Unattended semantics: runs under `bypass`; `unattended_trusted` and `unattended_approved` in session metadata; `answered_by: "unattended"`; `run_limit_reached`. | b6a66fcd7, d8e2a74ad, b4e567eed, f09de643d, 69a2e1220 | COVERED |
| `GET /v1/sessions/{sid}/usage` | b6a66fcd7 | COVERED |
| `POST`/`GET /v1/server/clients`, `DELETE /v1/server/clients/{client_id}`, `CLIO_STOP_WITH_LAST_CLIENT` | b6a66fcd7 | COVERED |
| The 0.3 session projection carries `metadata.agent_flow`. | 455dd0bd0 | COVERED |
| A file written by `ctx.workspace.write` becomes an artifact of the session. | 86cdaaaf0 | COVERED |
| `clio-agent -q` flags `--agent`, `--model`, `--workspace` | 2038b2f7f | COVERED |

### Connectors (#2358 unless noted). All MISSING.

- `GET /v1/connectors` (`?refresh=true`) returns `{connectors, diagnostics}`.
- `GET /v1/connectors/{id}` (`?refresh`, `?session_id=` for one conversation's values with `default_value` and `conversation` per need).
- `GET /v1/connectors/{id}/logo` (`?theme=dark`; SVG served with a restrictive CSP).
- `PUT /v1/connectors/{id}/values` `{values}`.
- `POST /v1/connectors/{id}/enable` and `/disable`.
- `POST /v1/connectors` `{url}` or `{command, args, name?}` adds a custom connector (201).
- `DELETE /v1/connectors/{id}` (custom only, else `connector_not_custom`).
- Commits: 7f44754fb, 9d719172e, 346348f27, 4cab50d89. The logo route appeared with 7f44754fb; the dark variant came with 4cab50d89.
- Error codes: `connector_not_found`, `connector_need_unknown`, `connector_value_invalid`, `connector_unreachable`, `connector_declined`, `connector_setup_incomplete`, `connector_values_set_for_conversation`, `connector_off`, `connection_name_taken`.
- Setup card: a pending interaction of kind `connector_setup`, answered at `POST /v1/sessions/{sid}/interactions/connector:<id>/respond` with `submit`+`values` (and `metadata.save_as_default`) or `decline`. It also covers the "agent needs" card on agent load (intent `agent_needs`: Enable all / Not now). Commits: 7f44754fb, 4559109d3, 157225e40.
- SSE events: `connector.changed` (global bus; payload `{connector_id, sessions}`) and `connector.setup.requested` / `connector.setup.resolved`. Commits: 7f44754fb, fcc9898ad.
- Sign-in and sign-out use the connector's address in use, and both publish `connector.changed`. Commits: 6f59b25c1, fcc9898ad.
- Plugin listing (`GET /v1/plugins`, the resolution view): rows gain `follows`, plus `connector {id, name, logo_url, logo_dark_url}` for a connector's plugin; "carrier" rows appear for enabled connectors. A session delta naming the plugin of a connector that is off drops it with `connector_off`. Commit: 9d719172e.
- Entry fields: `requires`, `tools` allowlist, `hide_tools`, need groups, optional needs. Commits: cf28e531d, 074a6ab59 (#2358); 5efe1afe5 (#2364).
- Catalog (`catalogs/connectors`, shipped in the wheel): Burette, SPICE, ParaView, PDF viewer, NASA Earthdata, Geo, Home Assistant, KiCad, IBM Quantum, plus every clio-kit server derived from its catalogue (including the 3D-print slicer). Commits:
  - e3d171d1e, e32557708, 1cbd8b60f, 680203f65, a818a6a0f, 73789e746, 4559109d3 (#2358)
  - 5efe1afe5 (#2364)
  - f51b64cb4 (#2359)
  - third-party connectors started through clio-kit: 9304bde10 (#2362)
  - The "3D globe" and "arXiv" entries were added and then removed inside the range, so the net change is none (703d90251 #2364, 1cbd8b60f).
- Agent tool `set_connector_value`. Commit: 7f44754fb.
- New config keys: `connectors.catalog_dirs`, `connectors.derive_kit`.

### Plugin offers (MISSING)

- `GET`/`PUT`/`DELETE /v1/plugins/offer-list`. GET returns the candidates `{key, kind, id, name, summary, logo_url, logo_dark_url, default_listed, listed, marketplace?|enabled?}` with `cap`, `total` and `listed_count`. PUT takes `{include, exclude}`; DELETE resets to the default. Errors are `offer_list_*` and `validation_error`. Config: `plugins.offer` in config.yaml, `plugins.offer_listed` (default 20). Commit: a8e4dca76 (#2364).
- Agent tool `offer_plugin`: offers any library plugin or connector that is off, needs a reason, and can search beyond the list by words or category. "Not now" counts as an answered offer. Commits: e87be94d7, 0eeb7e4a1, 90ff660a6, 6e7d18609 (#2358); 4a676cf2f, 3e40d2855, 6657aefc1 (#2364).
- Context view: new category "Plugins you can add" (`GET` context `categories`). Commit: 5ad03ad0f (#2364).

### MCP Apps in a tab and views (#2358 unless noted). All MISSING.

All routes are under `/v1/sessions/{sid}/mcp-apps/`. Before the range there were: GET `{app_id}`, `sandbox`, `tools/call`, `resources/read`, `model-context`, `messages` and DELETE.

- `PUT {app_id}/display-mode`, `POST {app_id}/downloads` (`gact.mcp_apps.download_max_bytes`; `download_too_large`, `download_link_unsupported`), `PUT {app_id}/pin`. Commit: 0116e2a02.
- `POST reopen` (replays the saved opening call; `mcp_app_calls` session metadata). Commits: 24db92e4d, 732b36ffb.
- `GET {app_id}/live`. Commit: fa26d783c.
- `PUT {app_id}/app-tools`, `POST {app_id}/app-tool-results/{call_id}`; the view's own tools become `app__<server>__<tool>`; SSE event `mcp_app_tool_call`, resent when a stream connects. Commits: 75ecae725, f28690eb5.
- `PUT {app_id}/model-context` moved module; path unchanged.
- Service proxy `{app_id}/service/{data_ref}[/{path}]`: HTTP (all methods) and WebSocket; `_meta["ai.iowarp.clio/service"]`. Commit: d3d46cee8.
- `PUT {app_id}/shown`, `POST {app_id}/looks/{call_id}`; agent tool `look_at_view`; SSE event `mcp_app_look`. Commits: 8d4a1984d, dc64c463b.
- SSE event `mcp_app_closed`; a view whose server stopped is not live. Commit: 63ce32010.
- Sandbox host for remote browsers: `gact.mcp_apps.sandbox_host`, 409 `sandbox_host_unconfigured`, trust refusal `sandbox_origin_refused`. Commits: 12105925e, 6d44784db.
- `mcp_app` part fields: `display_mode` and the label `"<connector>: <subject>"`. Commits: 0116e2a02, 96c417d3a, 0ca6ac04d.
- View calls carry `_meta["ai.iowarp.clio/origin"]="view"`. Commit: 11c8f1679.
- Comments on app views reach the agent. Commits: c10149b5f (#2358), 026670be3 (#2364).
- File views: `PUT /v1/sessions/{sid}/file-views/{view_id}`, `POST …/{view_id}/looks/{call_id}`; SSE event `file_view_look`; `file_view_*` refusals. Commit: 345a5a203 (#2358).
- A2UI `clio.mcp-app.v1`:
  - Refuses a non-view tool with `a2ui_mcp_app_tool_not_found` (4bb7c5f50).
  - Arguments are bounded (`a2ui_mcp_app_arguments_too_large` / `_too_deep`, 64 KiB); HTTP 422 from the A2UI route (a73c48e01).
  - `recordingUri` is exported as an artifact (8f2d11a6d, #2356).
- New config: `gact.mcp_apps.registry_limit`, `gact.mcp_apps.registry_ttl_s`.

### Stop and restart (#2354). All MISSING.

- `POST /v1/sessions/{sid}/cancel` takes an optional body `{"children": "keep"|"stop"}`; the default `keep` is unchanged, and extra keys are forbidden. Commit: 2731f9336.
- `GET /v1/sessions/{sid}/stop-scope` returns `{session_id, open_children[]}`. Commit: 2731f9336.
- `GET /v1/sessions/{sid}/tasks/{handle}/flow` gains `restarted_as`. A stopped run carries `stopped_by`, `stop_reason: conversation_stop` and `cancellation_by` / `cancellation_reason`. `restart_agent` answers `was_running`. Commit: 2f49e107b.
- Design doc: `docs/design/stop-and-restart.md`.
- A recovery reply's metadata carries `resumes_turn` on the v3 wire. Commit: 967c7713f (#2365).

### Storage and data (#2358). All MISSING.

- New connected-storage provider `s3` (Amazon S3, read-only, by bucket and prefix; anonymous, key pair or host profile). It appears in the storage-accounts list. Commit: 5b6f029fc.
- `GET /v1/sessions/{sid}/source-ranges/{handle}[/{name}]` (loopback only; `Range` gives 206, 416, 410) and `GET /v1/sessions/{sid}/source-range-stats/{handle}`. `connected_data_open range=true`; `range_unsupported`, `source_revision_changed`; `${CLIO_SOURCE_RANGE_ORIGIN}` for plugin servers. Commits: 16dd2604d, f4069193a.
- `POST` raster query body gains `select` (leading-dimension values, e.g. `time`) for NetCDF-4 time series. Commits: 783045580, 097714d5d.

### Report a problem (#2365). MISSING.

- `GET /v1/system/problem-report` (`lines` 1–5000, default 500; `session_id`, `include_messages`, `client=desktop|web`, `client_version`) returns redacted JSON plus `text`.
- `GET /v1/system/problem-report/download` returns it as a `.txt` attachment.
- `no_log_files` reason.
- Commit: acbd68349.

### Sandbox, tools and permissions. All MISSING.

- `shell_bash outside_sandbox` (+`access_reason`): always asks the person, allow once or deny; `outside_sandbox_not_approved`, `privilege_denied`. Administrator rights go through CLIO's card and then the OS prompt. Commits: e936a066a, 9054b86ab (#2364).
- Agent tool `fs_git_commit`. Agent file writes inside `.git` are refused with `git_metadata_protected`; `identity_unknown`. Commit: a138cd06b (#2305).
- Built-ins that act outside (`set_connector_value`, view tools) follow the approval mode. Commit: 305d27bfa (#2358).
- A permission card for an external MCP tool omits `execution` (computer/cwd). Commit: ab8fde3b6 (#2358).
- Permission prompts name MCP tools by title. Commits: 042a09343, 706266f60, f177aded7 (#2358).
- Servers may declare path arguments: `_meta["ai.iowarp.clio/paths"]`. Commit: e7007d012 (#2358).
- Oversized tool results are recorded as their head plus `stored_result {reason: tool_result_stored_in_file, …}` (`limits.tool_result_record_bytes`, 512 KiB). Commit: 4962733d1 (#2358).
- Structured content is bounded on the wire (`limits.tool_structured_bytes`; `structured_content_over_wire_cap`). Commit: bfcd5e5f7 (#2358).
- Identical tool calls in one reply run once (`tool_calls_collapsed`). "Prompt is too long" ends the turn with `context_window_exceeded`. Commit: 3ed323b6f (#2333).
- An MCP tool's image results reach the model. Commits: a4804b6e2 (#2358), 8619ee827 (#2364).

### Other additions. All MISSING.

- Blueprint/SDK `Agent.bare = True`: a bare agent whose model sees only the conversation. Commit: 1b316cb35 (#2329). It may be out of scope (agent authoring, not wire).
- Flows: `ctx.gather(merge_state=False)` (6f97cba73, #2351); `ctx.call(label=…)` names children in the `/flow` view (3ed323b6f, #2333); `ctx.stop_requested()` (2731f9336, #2354). These are flow-author API, so possibly out of scope.
- `literature_search`: ChemRxiv preprints through Crossref. Commit: cb13314bd (#2316).
- Attention: an alignment-failure reason now names the likely cause. Commit: 22ae981e9 (#2329).

## Changed (MISSING unless noted)

- `GET /v1/providers/{provider_id}/models` resolves a saved custom server `server-<slug>`; it used to answer 404. Commit: 32fdc878f (#2365).
- Session export `VisualReview`: the 30 MB `javascript` and 5 MB `stylesheet` caps were removed, so exports are no longer refused once the offline renderer grew. Commits: 91bbf76fc (#2364), 0e5627d99 (#2359).
- Messages and session creation answer `503 clio_core_writes_refused` while clio-core refuses writes (store full). This replaces `core_recording_unconfirmed`, `lane_replace_failed` and a bare 500. clio-core writes are refused below `arc.cte.metadata_disk_reserve` (256 MB) with `clio_core_metadata_disk_low`. Commits: 60b35d884, 903caaeaa, ef3341227 (#2358); e31f9c310 (#2365).
- A conversation with a tool call left without a result repairs itself (`operation_outcome_uncertain`) instead of failing every turn with `context_fold_failed`. Commit: 67d88461d (#2365, which cites #1282).
- Questions: an `ask_user` option given by id answers with that id; an option without a value answers with its label. Commits: bda92ed52, d29f4d3b5 (#2364).
- Anthropic API provider: runs on the native Messages engine, connects at the host root, and sends an output cap. Commits: 091d8df33, 01e749985 (#2351); 8d02be641 (#2352).
- Develop pins: `clio-kit==2.12.7` (COVERED but misfiled under Removed; see above) and `clio-schemas==0.8.0b1` (MISSING; carries the `clio.mcp-app.v1` `recordingUri` and the `s3` provider).

## Fixed (MISSING; a selection of the user-visible fixes)

- Restarting a stopped run is not refused over a cut-off call. Stop after a restart settles the tree instead of `task_state_uncertain`. A continuation keeps its request's behaviour. Commits: 82135eeb0, f2aef8dd7, ebb1bebde (#2363).
- A started or restarted flow's assignment settles when its run ends. Commits: e050b3901 (#2352), ea19d05ab (#2354).
- A crashed call's recorded result is restored to the transcript. Commit: 51b62df9d (#2360).
- A search question stays answerable while the person decides. Commit: 38028801a (#2364). Each approval question has its own input key: 89a0c33fa (#2336). Deep research may search the web and literature: eefffa373 (#2316).
- A NetCDF-4 HDF5 reference no longer fails the message. Commit: 1533840d9 (#2358).

## Unreleased entries that are inaccurate or incomplete

1. The two pin bullets sit under `### Removed` but describe pin changes. They belong under `### Changed`, as on main, and are not in v0.9.7.1 (see above).
2. "Removed: `routing_mode` in the Python client" leaves out the other client removals: `DEFAULT_BASE_URL`, the `base_url`/`headers` constructor parameters, and the fact that a client now attaches to or starts a server on construction.
3. The `flow` bullet says it is "checked with `run_flow`'s own check and its typed refusals" but names none of them. The codes are `flow_options_invalid` (listed under Changed), `flow_not_trusted` and `flow_not_resolved`.
4. The client docstring for `sessions.create` (`src/clio_agent/client/client.py`) still says unattended means "asks denied". The code and the changelog say a script run grants every permission (f09de643d). This is a code doc, not a changelog error.
5. Nothing else in Unreleased conflicts with the code. I checked: `/v1/server/clients` shapes, `/usage`, `metadata_reserved` on create and update (`message_marks.validated_session_metadata`), `AgentRef.id`, the `-q` flags, `ensure_server`, `agent_flow` projection, and the artifact-on-write behaviour.

## Not determined

- Route count: the guardrail test records 447 → 490 route-method pairs. The comment jumps 473 → 478 without naming those 5 routes. The source-range routes (3 GET; HEAD is implicit) plus 2 others are likely, but I did not resolve the exact 5.
- Whether these belong in the user changelog: flow-author API (`ctx.*`), `Agent.bare`, new config keys and env vars (docs/ENVIRONMENT.md: `CLIO_CONNECTOR_*`, `CLIO_MCP_APP_*`, `CLIO_PLUGINS_OFFER_LISTED`, `CLIO_TOOL_RESULT_RECORD_BYTES`, `CLIO_TOOL_STRUCTURED_BYTES`).
- I did not audit every new `mcp_app_*` / `file_view_*` refusal code individually; the families are listed above.
- I did not check gact-tui-side behaviour, which is pinned through submodule bumps.
