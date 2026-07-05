# 09 - Integration Plan: gact-tui <-> clio-agent

> Current integration status for using `gact-tui` as CLIO's primary terminal UI.

## Goal

Let an operator run:

```sh
uv run clio-agent-gact --host 127.0.0.1 --port 17800
GACT_BACKEND=http://127.0.0.1:17800 gact
```

and land in the GACT TUI conversation view against a live CLIO agent.

`gact agent deploy clio my-clio` remains the desired packaging UX, but the runtime contract is no longer a planned `/query` translator. CLIO now ships a native GACT backend.

## Current Topology

```text
gact-tui (Go/Bubble Tea)
  |
  | GACT /v1 REST + SSE
  v
clio-agent-gact (FastAPI, src/clio_agent/gact/app.py)
  |
  | DSPy + LiteLLM + CLIO experts + MCP tools
  v
ClioAgent
```

The legacy `clio-agent-api` process (`/query`, `/health`, `/experts`, `/metrics`) has been removed; its console script is now a deprecation shim pointing at `clio-agent-gact`. The GACT `/v1` surface below is the single TUI integration front door.

## Native GACT Mapping

| GACT primitive | CLIO endpoint | Current status |
|---|---|---|
| `GET /v1/health` | native GACT backend health | implemented |
| `GET /v1/capabilities` | native capability flags | implemented |
| `POST /v1/sessions` | native session store | implemented |
| `GET /v1/sessions` | native session list | implemented |
| `GET / PATCH / DELETE /v1/sessions/{sid}` | native session lifecycle | implemented |
| `POST /v1/sessions/{sid}/messages` | enqueue CLIO turn | implemented; returns before long LM work finishes |
| `GET /v1/sessions/{sid}/events` | native SSE event bus | implemented |
| `POST /v1/sessions/{sid}/cancel` | best-effort cancellation | implemented |
| `GET / PUT /v1/providers/lm` | inspect or hot-swap LM provider | implemented |
| `GET /v1/catalog/agents` | built-in, user, skill, and extracted agent definitions | implemented as catalog/definition surface |
| `GET /v1/catalog/tools` | tool catalog | implemented |
| permissions, hooks, context files, diffs, tasks, workspaces | native GACT endpoints | implemented with caveats tracked in `REAL_GAPS.md` |

## Streaming Semantics

The TUI should not translate legacy `/query?stream=true` `routing` /
`done` events for the main integration. It should consume
`GET /v1/sessions/{sid}/events` directly.

Expected event flow for a turn:

1. `POST /v1/sessions/{sid}/messages` accepts the user message.
2. `/events` emits `message.created` for user and assistant messages.
3. `/events` emits `message.part.added`, `message.part.delta`, and `message.part.completed` for assistant text.
4. `/events` emits `tool.call.started` / `tool.call.completed` when tool provenance is available.
5. `/events` emits `message.completed` with metadata, token/cost usage, and `error_info` when applicable.

Text delivery includes `stream_source`:

| `stream_source` | Meaning |
|---|---|
| `live` | Delta arrived through the live `dspy.streamify` path. |
| `batch` | Backend already had the final answer before live provider-token deltas could be emitted. |

Live streaming is best-effort: chat answers, provider-backed expert
synthesis, and registered user/skill agents attempt the live
`dspy.streamify` path when the upstream DSPy/LiteLLM stack emits chunks.
Paths that cannot start a live stream mark completed text as
`batch` with an explicit `stream_fallback.reason`; deterministic
non-token summaries may also use batch fallback metadata because there
are no provider tokens to stream.
The fallback object also carries `category`, `description`,
`recovery_actions`, legacy `synthetic_posthoc=true`, and `live_streaming=false`;
the allowed reason catalog is advertised as
`/v1/capabilities.capabilities.x_clio_stream_fallback_reasons`.
Once live stream execution has started, failures are surfaced as
structured `provider_error` turns rather than hidden sync reruns.

## Cancellation Semantics

`POST /v1/sessions/{sid}/cancel` is available. It settles the user-visible GACT envelope as cancelled and emits session status changes. If provider or tool work is already running inside an executor thread, the backend cannot guarantee a hard upstream abort; the event metadata marks this as `execution_cancellation="best_effort"` and `executor_work_may_continue=true`.

The TUI should render that as cancellation acknowledged, with no implication that the upstream provider request was killed.

## Remaining Real Gaps

Keep these visible to the engineering team rather than hiding them behind a normal-looking response:

- Streaming is live only where the upstream DSPy/LiteLLM path exposes
  answer chunks; otherwise batch fallback text remains a truthful
  fallback path.
- Tool lifecycle telemetry is live-only; post-turn `tools_called` remains summary metadata.
- Some GACT endpoint families are definition/catalog surfaces rather than full runtime routes for CLIO's core agent loop.

The authoritative tracker is `REAL_GAPS.md`; the broader capability table is `../CAPABILITIES_MATRIX.md`.

## Deployment Work Still Desired

The runtime is native, but packaging can still improve:

1. Add or keep `gact agent deploy clio my-clio` support in `gact-tui` so users do not have to start `clio-agent-gact` manually.
2. Probe for `clio-agent-gact` on `PATH` or run it through `uv`.
3. Pick a free local port, start the backend, probe `/v1/health` and `/v1/capabilities`, then register `GACT_BACKEND`.
4. Surface provider misconfiguration through the existing `/v1/providers/lm` settings flow instead of failing silently.

## Testing

- **CLIO unit/integration**: `tests/test_gact/` covers native GACT sessions, streaming, cancellation, provider config, catalog, permissions, diffs, metrics, hooks, context files, tasks, and workspaces.
- **Streaming/cancellation focus**: `uv run pytest tests/test_gact/test_streaming.py tests/test_gact/test_cancellation.py -q`
- **GACT TUI conformance**: run `gact-tui/contract/conformance` against a live `clio-agent-gact` port. Unsupported or partial areas should match `REAL_GAPS.md`, not be papered over with fake success states.
