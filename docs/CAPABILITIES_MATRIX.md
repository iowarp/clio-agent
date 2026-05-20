# Capabilities matrix — v0.3.1

This matrix records the capability flags advertised by
`/v1/capabilities` and the contract-level evidence behind each flag.
It does not claim that every true flag is driven by the real
`ClioAgent` on every possible turn path. For that release-readiness
audit, use `docs/tui/REAL_GAPS.md`.

**No silent downgrades.** If a flag is `true` here, the GACT API surface
exists in the released binary and has targeted evidence. The status column
distinguishes endpoint/wire verification from real `ClioAgent` runtime
driver verification. Partial runtime semantics, such as best-effort
cancellation, must be called out explicitly.
Two flags are explicitly `false` (LSP, voice — out of scope for v0.3.1).

## Core (v0.1)

| Capability | Status | How verified |
|---|---|---|
| `workspaces` | ✅ verified | `POST /v1/workspaces` + scoped session create; `clio_doctor_caps_final.png` |
| `sessions` | ✅ verified | full CRUD + fork + branching driven by integration suite |
| `subagents` | ✅ verified | Real expert `nanoagents_spawned` provenance is propagated through `ClioAgent.forward()` to GACT child sessions and `subagent.*` events; ARC invocation records retain the spawn rows. |
| `mcp` | ✅ verified | `/v1/mcp/servers` lists 3 bundled (fs/hdf5/parquet) + any installed third-party server; `clio_mcp_servers.png` |
| `files` | ✅ verified | context_files attach + influence agent answer (test_attached_context_file_influences_answer strict pass) |
| `diffs` | ✅ verified | Real planner `fs_propose_edit` tool observations are promoted into `file_diff` Parts; `/diffs/apply` writes the promoted `new_content` through file policy and `/diffs/reject` marks rows. Evidence: `tests/test_core/test_agent_planner.py::test_forward_promotes_propose_edit_observation_to_file_diffs`, `tests/test_gact/test_plan_edit_modes.py::test_real_agent_propose_edit_trace_becomes_applicable_diff`. |
| `permissions` | verified; runtime partial | Destructive MCP calls, direct third-party MCP calls through `/v1/mcp/servers/{server_id}/call`, and `/diffs/apply` enforce stored deny/allow policies before destructive execution. Direct third-party MCP calls can bind an explicit `session_id` for session-scoped policy and telemetry attribution. Remaining gap tracked in #218: finish the non-tool destructive API inventory and decide whether app-state deletes should share tool permission semantics. |
| `providers` | ✅ verified | swap haiku ↔ sonnet ↔ openrouter mid-session; cost-meter delta confirms model change |
| `commands` | ✅ verified | `/v1/commands` enumerates backend commands + TUI builtins (/mcp /tools /catalog /skills /agents-list /metrics /doctor /theme*) |
| `metrics` | ✅ verified | `/v1/metrics` rolls up per-session counts + tokens + cost; `clio_metrics.png` |

## v0.1 useful

| Capability | Status | How verified |
|---|---|---|
| `session_branching` | ✅ verified | `parent_session_id` set on forked sessions; `/v1/sessions/{sid}/fork` |
| `session_export` | ✅ verified | `/v1/sessions/{sid}/export` returns full JSON; replay round-trip works |
| `search_messages` | ✅ verified | `/v1/sessions/{sid}/messages/search?q=needle` returns hit; live test pass |
| `cost_tracking` | ✅ verified | every assistant message carries `tokens.input/output/cache_*` and `cost_usd`; live cost meter in TUI footer |
| `thinking_blocks` | ✅ verified | DSPy reasoning surfaces as `thinking` Parts on `ChainOfThought` predictions |
| `session_tasks` | ✅ verified | `/v1/sessions/{sid}/tasks` CRUD + per-task lifecycle |

## v0.2 additions

| Capability | Status | How verified |
|---|---|---|
| `agent_routing` | ✅ verified | `routing_decision` Part lands on every assistant message; visible in screenshots |
| `memory` | ✅ verified | ARC cache stats reported on `/v1/memory/stats`; `clio_doctor_health.png` shows hit rate |
| `structured_errors` | ✅ verified | every 4xx/5xx returns the v0.2 envelope (error/message/details/recoverable) |
| `integration_health` | ✅ verified | `/v1/health.integrations[]` reports per-subsystem status; `clio_doctor_health.png` |
| `tool_telemetry` | ✅ verified | Tool lifecycle events are emitted only from live execution observers (`telemetry_source="live_observer"`). Post-turn `tools_called` traces remain assistant metadata summaries and are not reconstructed into fake lifecycle events. Evidence: `tests/test_gact/test_tool_telemetry.py`, `tests/test_integration_v0_2/test_real_capabilities.py::test_real_tool_call_events_fire_during_turn`. |

## Transport Truthfulness

`transports.events_sse=true` means the backend emits the GACT event
stream. It does not mean every text delta is a live token from the
provider.

Text streaming has two explicit modes on `message.part.*` payloads and
assistant `message.completed.metadata.stream_source`:

| `stream_source` | Meaning |
|---|---|
| `live` | Delta arrived through the live `dspy.streamify` path. |
| `synthetic_posthoc` | Backend already had the final answer before it could emit live provider-token deltas. |

Synthetic post-hoc text is delivered as a completed text part, not as
fake `message.part.delta` chunks. The text part metadata and assistant
`message.completed.metadata` include `stream_fallback.reason` so clients
can explain why live provider token streaming was not used.
`stream_fallback` also includes `category`, `description`,
`recovery_actions`, `synthetic_posthoc=true`, and `live_streaming=false`.
The audited reason catalog is advertised in
`/v1/capabilities.capabilities.x_clio_stream_fallback_reasons`; unknown
fallback reasons are rejected instead of silently creating a new
semantics bucket.
Failures while executing the live stream surface as structured
`provider_error` turns, before or after visible output; they are not
rerun through the sync path to create synthetic post-hoc answer text.

As of v0.3.1, chat answers, provider-backed expert synthesis, and
registered user/skill agents attempt live streaming when the upstream
DSPy/LiteLLM provider supports it. Paths that cannot start a live stream
still label completed text as `synthetic_posthoc` with an explicit
`stream_fallback.reason`, but they do not emit synthetic delta events. If
streaming starts but produces only a final prediction and no visible
chunks, the reason is `stream_completed_without_chunks` rather than the
generic sync fallback. Deterministic non-token summaries may also be
synthetic because there are no provider tokens to stream.

Cancellation is also best-effort at the GACT boundary. A cancelled turn
settles with `error_info.error="cancelled"` and status events include
`execution_cancellation`. GACT now passes a cooperative cancellation
checker into compatible agent turns, and the sync MCP bridge checks for
cancellation before reporting normal tool success. If a provider or tool
call is already running inside an executor thread and cannot observe the
checker in time, the server reports `execution_cancellation="best_effort"`
and `executor_work_may_continue=true`; clients must not interpret that as
a guaranteed upstream abort.

## Vendor-specific (CLIO additions on top of v0.2)

| Capability | Status | How verified |
|---|---|---|
| `scheduled_sessions` | ✅ verified | cron `* * * * *` schedule fired in 27s; `fire_count=1`, `last_fired_at` updated |
| `hooks` | ✅ verified | `~/.config/clio-agent/hooks/post_message.py` fired on real turn; `/tmp/hook-fired` marker created |
| `session_sharing` | ✅ verified | `/v1/sessions/{sid}/share` issues `shr_…` token; `/v1/shared/{token}` returns the session |
| `edit_modes` | ✅ verified | session.edit_mode (diff/whole/patch) shapes the file_diff Part — diff: unified_diff, whole: new_content only, patch: both |
| `plan_mode` | ✅ verified | session.mode=plan refuses `/diffs/apply` with `PermissionError("refused to write under session.mode='plan'")`; file unchanged |
| `agent_write` | ✅ verified | `POST/PUT/DELETE /v1/agents` lifecycle; user agents appear in `/v1/agents` with `source="user"`; prompt-only agents execute through DSPy/LiteLLM, and tool-declaring agents execute through a DSPy ReAct runner scoped to their declared MCP tools. |
| `skills_extraction` | ✅ verified | `POST /v1/agents/extract` mines `tools_called` from past sessions → produces a user agent definition visible in `/v1/agents`; extracted agents execute with prompt-only or declared-tool semantics. |
| `x_clio_text_streaming` | best-effort live | `best_effort_live` means live provider-token streaming is attempted. `x_clio_synthetic_posthoc_streaming=false` means completed fallback text is delivered as a normal text part rather than fake streaming chunks. `x_clio_stream_fallback_reasons` lists every allowed synthetic post-hoc reason and recovery action. |

## Provider-Specific Verification

| Provider path | Status | How verified |
|---|---|---|
| `codex` / `exec` | ✅ verified | `CLIO_LM_PROVIDER=codex CLIO_CODEX_TRANSPORT=exec uv run --extra codex src/clio_agent/ui/cli.py --query ... --json` returned the requested sentinel through `route_source="dspy"` with `error_info=null`. |
| `codex` / `sdk` | ✅ verified | `CLIO_LM_PROVIDER=codex CLIO_CODEX_TRANSPORT=sdk uv run --extra codex src/clio_agent/ui/cli.py --query ... --json` returned the requested sentinel through `route_source="dspy"` with `error_info=null`. |

## Intentionally out of scope (false)

| Capability | Status | Why |
|---|---|---|
| `lsp` | ⛔ false | CLIO is a scientific-data agent, not a code editor. LSP doesn't fit the data-analysis loop. |
| `voice` | ⛔ false | Voice IO requires platform-specific audio plumbing (ffmpeg/whisper/etc.) outside CLIO's scope. Future flag may flip when we adopt a voice provider. |

**28 / 30 advertised supported.** Flag inventory matches `/v1/capabilities` and the wire shapes match SPEC section 6. Rows marked `runtime partial` are not release-readiness proof by themselves; real-agent driver caveats remain tracked in `docs/tui/REAL_GAPS.md`.
