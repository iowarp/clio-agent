# Capabilities matrix — v0.3.1

This matrix records the capability flags advertised by
`/v1/capabilities` and the contract-level evidence behind each flag.
It does not claim that every true flag is driven by the real
`ClioAgent` on every possible turn path. For that release-readiness
audit, use `docs/tui/REAL_GAPS.md`.

**No silent downgrades.** If a flag is `true` here, the GACT API surface
exists in the released binary and has targeted evidence. Partial runtime
semantics, such as post-hoc tool telemetry or best-effort cancellation,
must be called out explicitly.
Two flags are explicitly `false` (LSP, voice — out of scope for v0.3.1).

## Core (v0.1)

| Capability | Status | How verified |
|---|---|---|
| `workspaces` | ✅ verified | `POST /v1/workspaces` + scoped session create; `clio_doctor_caps_final.png` |
| `sessions` | ✅ verified | full CRUD + fork + branching driven by integration suite |
| `subagents` | ✅ verified | `analysis_validator subagent` rows under parent → `clio_subagent.png` |
| `mcp` | ✅ verified | `/v1/mcp/servers` lists 3 bundled (fs/hdf5/parquet) + any installed third-party server; `clio_mcp_servers.png` |
| `files` | ✅ verified | context_files attach + influence agent answer (test_attached_context_file_influences_answer strict pass) |
| `diffs` | ✅ verified | propose-edit produces file_diff Part; apply changes file on disk; reject leaves it; audit row recorded — `clio_diff.png` |
| `permissions` | ✅ verified | every destructive write logs an audit row in `/v1/permissions` (test_destructive_tool_requests_permission strict pass) |
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
| `tool_telemetry` | ✅ verified | `tool.call.started/completed` SSE events fire BEFORE `message.completed` (strict integration test) |

## Transport Truthfulness

`transports.events_sse=true` means the backend emits the GACT event
stream. It does not mean every text delta is a live token from the
provider.

Text streaming has two explicit modes on `message.part.*` payloads and
assistant `message.completed.metadata.stream_source`:

| `stream_source` | Meaning |
|---|---|
| `live` | Delta arrived through the live `dspy.streamify` path. |
| `synthetic_posthoc` | Backend already had the final answer and chunked it after completion for TUI rendering continuity. |

As of v0.3.1, the chat `answer` path can stream live when the upstream
DSPy/LiteLLM provider supports it. Expert paths that do not expose that
`answer` stream still fall back to `synthetic_posthoc`.

Cancellation is also best-effort at the GACT boundary. A cancelled turn
settles with `error_info.error="cancelled"` and status events include
`execution_cancellation`. If a provider or tool call is already running
inside an executor thread, the server reports
`execution_cancellation="best_effort"` and
`executor_work_may_continue=true`; clients must not interpret that as a
guaranteed upstream abort.

## Vendor-specific (CLIO additions on top of v0.2)

| Capability | Status | How verified |
|---|---|---|
| `scheduled_sessions` | ✅ verified | cron `* * * * *` schedule fired in 27s; `fire_count=1`, `last_fired_at` updated |
| `hooks` | ✅ verified | `~/.config/clio-agent/hooks/post_message.py` fired on real turn; `/tmp/hook-fired` marker created |
| `session_sharing` | ✅ verified | `/v1/sessions/{sid}/share` issues `shr_…` token; `/v1/shared/{token}` returns the session |
| `edit_modes` | ✅ verified | session.edit_mode (diff/whole/patch) shapes the file_diff Part — diff: unified_diff, whole: new_content only, patch: both |
| `plan_mode` | ✅ verified | session.mode=plan refuses `/diffs/apply` with `PermissionError("refused to write under session.mode='plan'")`; file unchanged |
| `agent_write` | ✅ verified | `POST/PUT/DELETE /v1/agents` lifecycle; user agents appear in `/v1/agents` with `source="user"` |
| `skills_extraction` | ✅ verified | `POST /v1/agents/extract` mines `tools_called` from past sessions → produces a routable agent |

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

**28 / 30 advertised supported.** Flag inventory matches `/v1/capabilities` and the wire shapes match SPEC section 6. Real-agent driver caveats remain tracked in `docs/tui/REAL_GAPS.md`.
