# Changelog

All notable changes to clio-agent's GACT-contract surface are
documented in this file. Internal changes that don't affect the
TUI/HTTP surface aren't tracked here.

## Unreleased

### Changed
- **Mains are react agents; the settle/synthesis layer is deleted** (#948 S4 /
  #952). A tier-1 main runs the retained ReAct loop and its `answer` IS the
  user deliverable; it routes by CALLING the spawn-runtime tools
  (`spawn_agent_task` / `wait_agent_tasks` / `check_agent_tasks` /
  `spawn_agents_parallel`) over real child turns. The wire-facing
  `blueprint.delegation.{started,completed,failed}` / `blueprint.fanout.started`
  events are re-emitted by the spawn runtime (event types unchanged); the
  completed payload now carries `task_id` / `message_ref` / `error_reason`
  alongside `output` / `workflow_state` / `stage` and no longer carries
  `return_to` / `tools_called` / `structured` (those live on the AgentTask
  record and the `agent.task.*` events).
- Typed blueprint validation: an expert with declared children must declare
  `module.kind: react` (children are reachable only via the spawn-runtime
  tools); `predict` / `chain_of_thought` remain valid for leaf experts only.
- Blueprint expert `answer` output is REQUIRED again — the optional-answer
  default existed so an orchestrator could defer its deliverable to a
  synthesis child; that pathway is deleted.
- Session agent-overlay export now round-trips the `module:` declaration
  (previously dropped, so an exported react parent re-loaded as `predict` and
  failed validation).

### Removed
- The settle/synthesis orchestration internals (#948 S4 / #952): the settle
  loop + parent re-invoke resume prompts (`turn_delegation.py`,
  `turn_delegation_arc.py`), the `final_responder` synthesis-child adoption
  (`turn_terminal.py`) and its degradation reasons, `answer_stream_visible`,
  the nanoagent post-hoc materialization (`turn_nanoagents.py`), the
  `next_expert`/`next_task` typed routing signature fields (internal, never
  wire fields), and the inline per-child delegate / fan-out tools. Marketplace
  packs are migrated (mains → react, synthesis children deleted) and the
  submodule pin updated. A baseline-0 CI guard
  (`scripts/check_no_settle_vocabulary.py`) keeps the vocabulary out.

### Added
- Child-turn substrate (#948 S3 / #951): `spawn_child_turn` spawns a declared child
  expert as a REAL turn in a REAL child session (projected as an `AgentTask`), on a
  dedicated executor pool (never the default), with depth/declared-child guards,
  FIFO queue admission at the concurrency cap, a completion hook that records the
  child's result, HITL-in-child typed failure, and a parent→children cancel cascade.
  The `#671` federation seam (`TaskSpec` serializable in/out).
- Agent-task API + event family (#948 S2 / #950): `GET /v1/sessions/{sid}/agent-tasks`,
  `GET /v1/agent-tasks/{task_id}`, `POST /v1/agent-tasks/{task_id}/cancel`, and the
  `agent.task.{queued,started,completed,failed,cancelled,consumed}` events
  (published on both the parent and child session channels). The `AgentTask`
  record projects over a child session's metadata (`session_type=="agent_task"`)
  — no new store — and its registry is rebuilt at boot from `sessions.json`.
  The paths are `agent-tasks`: `/v1/sessions/{sid}/tasks` + `/v1/tasks/{tid}`
  remain the #18 per-session manual task CRUD; S2's original same-path claim
  shadowed that GET by registration order (fixed in S4).

## [0.7.4] — 2026-07-15

### Added
- Skill semantics with progressive disclosure (#916): experts get a tier-1
  skill *metadata* block plus a model-invoked `load_skill` tool (full bodies
  load on demand, never eagerly); `skill.loaded` provenance events stream on
  the wire, and turn provenance carries `resolved_skills` /
  `skill_resolution` so clients can render declared-skill status
  (gact-tui#315 is the client half).

### Removed
- **Legacy-path deletion** (#775 no-accretion; the strangler flags
  became the only semantics): the classic `_RetainingReAct` expert loop
  (`CLIO_REACTV2`), the parallel working-set write (`CLIO_ARC_WORKING_SET_FOLD`),
  the legacy transcript regime + per-session pin (`CLIO_TRANSCRIPT_PROJECTION`),
  the stateful-delta kill-switches (`CLIO_CLAUDE_CODE_STATEFUL_DELTA`,
  `CLIO_CODEX_STATEFUL_DELTA`), the claude_code `exec` batch transport
  (one `claude -p` per call), the codex `exec`/`sdk` batch transports, and
  the codex app-server kill-switch (`CLIO_CODEX_APP_SERVER`) with its
  `app_server_kill_switch` downgrade reason. All six env knobs are gone
  from `docs/ENVIRONMENT.md` / `.env.example`.

### Changed
- `PUT /v1/providers/lm`: `transport` accepts only `sdk` (claude_code) /
  `app_server` (codex); a deleted transport value returns a typed 400
  (`removed in the v0.8.0 cleanup`), never a silent downgrade. The route
  no longer leaked the codex transport default into non-codex binds, and
  the codex catalog preset's registry marker moved `codex://exec` →
  `codex://app-server` (the stale marker silently steered provider swaps
  onto the deleted batch path).
- `turn.completed` durable payloads never embed `final_message` on an
  ARC-backed server (atoms are the only transcript regime); the embed
  survives only in the no-ARC structural case.
- Typed-output repair for react experts now re-drives a forced `submit`
  over the retained History (the V2 repair) from the builders repair
  ladder; the classic extract-only re-run is gone.
- MCP fleets are memory-bounded (#930, #942; server-internal, no wire
  change, noted for operators): namespaces spawn on first tool call, boot
  reads tool definitions from a launcher-anchored cache (warm boots spawn
  zero server processes; cold boots list one namespace at a time), idle
  workspace fleets are reclaimed (TTL + LRU, typed reap reasons), stable
  clio-kit launchers respawn as direct venv interpreters, and the
  3-session acceptance load is budget-gated at release time
  (3.57 GB → 1.42 GB cold peak / 0.72 GB settled).

## [0.7.3] — 2026-07-14

### Fixed
- Portable-runtime prune, final pass: `python*-config` symlinks removed
  (the macOS leg died on a dangling `python3-config` after its target was
  pruned — and the GNU-only `-xtype` sweep silently no-opped on BSD find,
  our own silent-fallback class; the sweep is now portable), and
  distlib/setuptools Windows launcher stubs are pruned from site-packages
  (a 180KB `t64-arm.exe` inside the packed runtime masqueraded as the
  bundled installer and failed the 60MB payload floor on the linux legs).
  Linux validation in-tree before tagging: relocated boot green, zero
  dangling links, zero stray exes.

## [0.7.2] — 2026-07-14

### Fixed
- Portable-runtime prune left dangling `bin/` symlinks (e.g. `2to3` after
  its `2to3-3.12` target was deleted) that failed Tauri's resource walk on
  the unix bundled legs; the prune now removes non-interpreter symlinks
  and sweeps any dangling link. Validated end-to-end on Linux (WSL):
  relocated boot + `/v1/capabilities` green before tagging.

## [0.7.1] — 2026-07-14

### Fixed
- Release engineering: v0.7.0's tag pinned gact-tui at v0.9.6 (a rejected
  tag-fetch silently aborted the &&-chained pin checkout), so its bundled
  desktop legs failed loudly on the retired `clio-runtime` resources glob
  — the guard doing its job. v0.7.1 pins gact-tui **v0.9.7** (verified
  gitlink) and re-ships the identical payload; PyPI 0.7.0 artifacts were
  unaffected (wheel/sdist carry no submodule content).

## [0.7.0] — 2026-07-13

The second resource-usage campaign release (#893): the unified-ARC
highway ships as the default, the desktop bundle works on a fresh
computer end-to-end, and clio-agent's memory is hard-bounded. Pairs
with gact-tui **v0.9.7** and marketplace **v0.5.4**.

### Changed
- **Hard memory budget** (#906, release-gating): clio-core's storage
  runs a disk-only desktop topology — ONE pre-created RAM arena bounded
  at the user's memory budget (`arc.cte.ram_capacity` /
  `CLIO_ARC_CTE_RAM_CAPACITY`, default 1GB) as working memory, and ONE
  file tier at the user-designated dir (`arc.cte.dir`) holding all data.
  "Use 1GB of RAM, and whatever you want of disk." clio-core's own `0g`
  default (up to 80% of system DRAM — an HPC compute-node default) can
  no longer be inherited silently: boot-time typed warnings
  (`clio_core_ram_uncapped`, `clio_core_tier_topology`) and doctor rows
  flag unbounded arenas, legacy two-arena shapes, and undersized final
  layers. Live gate evidence: daemon peak 574MB under the 1GB bound
  through three full EarthScope sessions.
- clio-core blob writes ride a bounded, typed-loud retry
  (`clio_core_put_retry`, 3 attempts): transient PutBlob refusals (an
  eviction race, a post-restart container-restore gap, disk exhaustion —
  all caught live on the #893 gate) no longer turn one failed write into
  a failed turn under the atoms regime's must-succeed ingest.
- The unified-ARC highway regimes are the shipped defaults (#737, #893):
  the working-set fold (`CLIO_ARC_WORKING_SET_FOLD`) and the transcript
  projection (`CLIO_TRANSCRIPT_PROJECTION`) default ON — new sessions run
  single-copy on the canonical `_events` log (byte-equality proven per
  surface; reload==live green on the full real-session corpus). `=0` opts
  back into the legacy regime; existing sessions keep their pinned regime
  and their wire stays byte-identical.
- Stateful session-delta transports default ON for `claude_code` (SDK
  transport) and `codex` (app-server): `CLIO_CLAUDE_CODE_STATEFUL_DELTA` /
  `CLIO_CODEX_STATEFUL_DELTA` `=0` opt out. Live acceptance: delta TTFT
  1.79s vs 7+s cold (claude), 2.95s vs 7.37s (codex), 76.7% cached-input;
  every fallback-to-full-send is a typed reset reason.
- Boot-time environment conformance (#906): clio-core backend init inspects
  the EFFECTIVE `cte.yaml` and emits a typed `clio_core_ram_uncapped`
  warning when the ram cap is unbounded/unparseable/missing (the stale-0g
  incident shape); the doctor ram-cap row also reports the ram bdev
  capacity.

### Fixed
- The bundled desktop runtime is machine-portable (#909): the embedded
  clio-agent now ships its own interpreter (uv's standalone CPython copied
  into the runtime, wheel installed directly — no venv) and self-describes
  via a generic `runtime.json` manifest, invoked as `python -m
  clio_agent.gact`. The previous venv-based runtime hard-pointed
  `pyvenv.cfg home` at the CI runner's Python and could never start on a
  user's machine. `install/build-gact-runtime.{sh,ps1}` build it (moved
  here from gact-tui — the brand owns its runtime) and prove portability
  by booting a relocated copy; the bundle workflow builds the runtime from
  the released checkout and gates on the manifest being present.
- Desktop bundles ship their sidecar launcher again (#907): the CLIO branding
  overlay now declares `bundle.externalBin`, and the bundle workflow asserts
  the merged Tauri config + built launcher before packaging. Every desktop
  installer since v0.5.18 (first post-de-clio gact-tui pin) was missing the
  launcher and failed at boot with "sidecar launcher missing". Pairs with the
  gact-tui lookup fix (iowarp/gact-tui#309) — the installed sidecar is
  triple-stripped by the Tauri bundler. Known residual: the Windows-on-ARM
  leg publishes a raw `--no-bundle` exe with no launcher beside it and
  remains unfixed (noted in #907).

## [0.6.1] — 2026-07-13

### Fixed
- Release container images actually publish: the Docker workflow now runs on
  `v*` tags (its push-to-ghcr step was tag-gated but the workflow had no tag
  trigger, so release images were silently never pushed). v0.6.0's images
  ship under this tag.

## [0.6.0] — 2026-07-13

The resource-usage campaign release. Pairs with gact-tui **v0.9.6**
(pinned submodule) and marketplace **v0.5.4**.

### Added
- `PUT /v1/providers/lm` accepts `thinking_level` (`off|low|medium|high`;
  out-of-vocabulary values are a structured 422). `GET`/`PUT` responses
  report `thinking_level` and the resolved `thinking_effective`
  (including the typed `unsupported (<reason>)` form). SPEC §6.12 (#895).
- `LMProviderInfo.transport` may now report `app_server` — the codex
  warm app-server transport (#896).

### Changed
- The expert loop is dspy ReActV2 (append-only History; provider prompt
  prefix byte-stable across iterations). The SSE wire, semantic-event
  stream, and tree view are contract-identical (byte-equality suites on
  both paths); `CLIO_REACTV2=0` selects the classic loop (#901).
- Typed degradation reasons and doctor probe names that referred to the
  clio-core system as "CTE" are renamed `clio_core_*` (`CTEStore` →
  `ClioCoreStore`, `cte_daemon_not_listening` →
  `clio_core_daemon_not_listening`, etc.). CTE now names only the
  actual clio-core component (config artifacts unchanged).

### Fixed
- Blueprint install from `file:///C:/...` marketplace URIs works from
  native Windows processes (#903).
- The earthscope pack no longer misreports geo-filter tool failures as
  "no coverage"; the no-coverage terminal requires structural filter
  evidence (marketplace v0.5.4, #904).

## [0.5.18] — 2026-07-09

The grounding-demo release. Pairs with gact-tui **v0.9.5** (pinned
submodule) — #233 render parity + #232 protocol convergence on the
client side, matched by the contract-surface changes below on the
server side.

### Added
- `GET /v1/sessions/{id}/messages` now honors pagination
  (`before` / `limit` / `include_system`) and `GET /v1/sessions`
  honors the `parent_session_id` filter. These params were previously
  accepted but ignored (unbounded payloads; subsession UIs saw every
  session); they are now a normative contract with conformance
  coverage on the gact-tui side (#232, #872).

### Changed
- Compaction emits a **structured compaction part** (a typed part
  carrying the summary + metadata) instead of `[compact summary]`
  prose, so clients render it as a first-class part rather than
  scraping text (#832, #873).

### Removed
- Retired the legacy Codex HTTP bridge process. Switch
  `provider=codex` to the LiteLLM CustomLLM path; no bridge process is
  needed. Users pointing at `:18900/v1` should re-pick the `codex`
  preset so config resolves through the registered provider.

> **Tracking gap:** releases `0.3.2` through `0.5.17` were shipped
> without a CHANGELOG entry. For those versions, see the GitHub release
> notes at https://github.com/iowarp/clio-agent/releases. This file is
> owned by the release skill going forward (see
> `.claude/skills/release-clio/SKILL.md`).

## [0.3.1] — 2026-04-27

The "every advertised capability actually works" release. Every flag
in `/v1/capabilities` that's `true` has been verified end-to-end with
either an integration test, a curl trace, or a screenshot — see
`docs/archive/CAPABILITIES_MATRIX.md` for the full matrix. **No silent
downgrades anywhere; if a flag is true, it works.**

### Added
- **Third-party MCP install + use endpoints** (#13).
  - `POST /v1/mcp/servers` (stdio: npx/uvx/raw command, OR http URL).
  - `POST /v1/mcp/servers/{id}/call` (invoke a tool on the installed server).
  - `DELETE /v1/mcp/servers/{id}` (uninstall).
  - `GET /v1/mcp/servers` returns BOTH bundled (fs/hdf5/parquet) AND
    installed third-party servers in one list.
  - Verified end-to-end against `@modelcontextprotocol/server-everything`:
    13 tools enumerated, `echo` + `get-sum` round-tripped via stdio.
- `docs/archive/CAPABILITIES_MATRIX.md` — one row per advertised capability with
  the proof for each (curl evidence / screenshot / integration test).
- New screenshots: `clio_mcp_servers.png` (bundled + third-party), 
  `clio_diff.png` (apply/reject path).

### Fixed
- **Generic tool observer (#2 closed strict).** `_call_tool_function`
  fires the global tool_observer for EVERY tool path — bundled
  in-process, ReAct via MCPToolBridge, AND third-party MCPs. SSE
  events `tool.call.started`/`completed` now arrive before
  `message.completed` for every tool call. `tools_called` metadata
  populated automatically.
- **Context files actually influence answers (#5 closed strict).**
  Removed silent workspace-root filter on attached files (user
  explicitly attached → trust them; write gates still apply via
  `_apply_edit_to_disk`). Binary files (parquet/hdf5) now inlined as
  schema summaries via the bundled tool inspectors instead of useless
  raw bytes.
- **Streaming deltas verified arriving in lifecycle order (#6 closed strict).**
  ClioAgent.acall added so streamify uses `is_async_program=True`
  instead of asyncify-in-executor (which was stripping the send_stream
  ContextVar). Single StreamListener bound to `answer` for clean chat
  output. Live per-token timing depends on the upstream provider —
  some OpenAI-compatible gateways buffer, OpenRouter passes through.
- **Permission audit trail (#7 closed strict).** `_apply_edit_to_disk`
  records an auto-approved permission row (action=allow,
  reason=`user_clicked_apply`) for every diff/apply. `/v1/permissions`
  has a complete audit trail of every destructive operation.
- **Plan mode + edit_modes thread into agent.** `ClioAgent.forward`
  gains `session_mode` + `session_edit_mode` kwargs; `_direct_edit_answer`
  shapes the file_diff Part by mode (diff: unified_diff,
  whole: new_content only, patch: both).
- **Explicit-edit short-circuit.** `_looks_like_explicit_edit` detects
  "propose an edit to /path/file.ext" and bypasses the router so the
  edit handler always fires regardless of which expert the file's
  extension would otherwise pick.
- **fs_server validate signature.** `validate_non_empty_string` was
  being called positionally where it declared `field` as keyword-only.
- **Edit-intent honored across providers.** `_direct_chat_completion`
  timeout 60s → 180s for slower providers.
- **Test infra reliability.** `tests/test_integration_contract/conftest.py`
  httpx client timeout 30s → 90s to absorb proxy tail latency.

### Performance
- Full integration_contract suite is 16/16 strict in ~95s (was ~25min before
  the streaming + adapter fixes earlier in this release cycle).

### Removed
- All `@pytest.mark.xfail` decorators in
  `tests/test_integration_contract/test_real_capabilities.py`. Suite is
  now strict-only.

## [0.3.0] — 2026-04-25

### Added
- GACT contract v0.2 implementation (28/30 capabilities supported;
  only LSP + voice intentionally out of scope).
- Workspaces — group sessions by project root (#12).
- Backend-provided slash commands (#14) and provider listing (#15).
- Session export to JSON (#16) + per-session task tracking (#18).
- Dynamic agent registry — POST/PUT/DELETE on `/v1/agents` (#19).
- User-defined hook subsystem (`pre_tool`, `post_tool`,
  `pre_message`, `post_message`, `on_error`) loaded from
  `~/.config/clio-agent/hooks/<event>.py` (#20).
- Scheduled session turns via cron expressions (#21).
- Session sharing with TTL tokens (#22).
- Skills extraction from past sessions (#23).
- DSPy reasoning trace surfaced as `thinking` Parts (#17).
- LM provider preset for direct OpenAI / ChatGPT (`gpt-4o-mini`
  default) — most-asked-for setup in lab usage.
- `/doctor` Capabilities scorecard tab on the TUI side, sourced
  from `/v1/capabilities`.
- TUI `lm_config` modal exposes Temperature + Max tokens fields
  alongside provider/model/key.

### Changed
- DSPy LM cache is now disabled by default (`cache=False`). Real
  serving wants accurate token accounting; identical-prompt cache
  hits were short-circuiting `dspy.settings.usage_tracker` and
  reporting zero. Trade-off: identical prompts cost real money each
  time. Filed as a setup note in `docs/SETUP.md`.
- `dspy.configure(...)` async-task-ownership guard side-stepped on
  PUT `/v1/providers/lm` so the LM can be swapped mid-session
  (haiku → sonnet → openrouter) without a server restart. Verified
  with cost-meter delta confirming the swap took effect at the LM
  call layer.
- ChatAdapter (with JSONAdapter fallback for cloud providers) wired
  at boot + on PUT `/v1/providers/lm`. Without it, Claude often
  returns plain text and DSPy's default JSONAdapter chokes with
  `LM Response cannot be serialized to a JSON object`.
- Streaming now uses `dspy.streaming.StreamListener` filtered to the
  `answer` field. Previously the user-visible text Part included
  ChatAdapter delimiter markers (`[[ ## answer ## ]]`,
  `[[ ## reasoning ## ]]`) and the router's chain-of-thought trace
  bleeding through.
- Token + cost accounting now snapshots `lm.history` across every
  known LM (main + `_router_lm`) and diffs the slice per turn.
  Required because DSPy contextvars don't propagate to asyncio
  executor threads, so the per-turn `UsageTracker` was unreliable
  from the request handler.
- `message.part.completed` event payload now ships `final_text` so
  the TUI can replace the streamed buffer (which still carries
  upstream markers from the underlying litellm chunks) with the
  parsed clean answer once the part is done growing.

### Fixed
- Router LM occasionally returned malformed Literal values
  (`"None"` as a string, empty strings) which crashed the dispatch
  with "I encountered an issue with the None expert". Coerced to
  one of the known buckets (data/analysis/visualization/none/chat),
  defaulting to chat.
- Doubled provider prefix (`openai/openai/claude-haiku-4-5`) when
  the API was sent a model id that already included the prefix.
  Strip + reapply once.
- 5 of 16 integration tests in `tests/test_integration_contract/` were
  flaking on proxy tail latency. Bumped LM-driven turn timeouts
  from 120s to 300s. Suite is now 11 passed + 5 honest xfails (each
  xfail links the GitHub issue tracking the gap).

### Performance
- Integration suite runtime dropped from ~25 minutes to ~6 minutes
  end-to-end after the streaming + adapter fixes — likely because
  StreamListener avoids buffering router noise + ChatAdapter
  prevents JSON-parse retries.

### Removed
- Old `selected = (None or "chat")` fragility that produced "the
  None expert encountered an issue" answers — see Fixed.

## [0.2.0] — 2026-04-22

Initial v0.2 contract scaffolding:
- `src/clio_agent/gact/` Python module wrapping `ClioAgent` as a
  GACT v0.2 server. FastAPI + SSE.
- `clio-agent-gact` console script (`uv run clio-agent-gact --port
  17800`).
- v0.2 capabilities flipped on as features landed: `sessions`,
  `agent_routing`, `memory`, `structured_errors`,
  `integration_health`, `tool_telemetry`, etc.

(Full details in the gact-tui integration plan items #1–#23.)
