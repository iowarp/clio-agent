# Real-readiness gap log

Honest list of what's wire-shape-only vs end-to-end-working when CLIO
runs against a real LM (Anthropic Claude direct, OpenRouter + a free
model). Drives what to fix before declaring v0.2 ready.

Updated as gaps land or close.

## Hard blockers — must fix before "ready"

### 1. Tool execution hangs

DataExpert's `MCPToolBridge` doesn't return through the executor
path. A `data` route times out client-side; server stays stuck
mid-`forward()`. Symptoms include
`coroutine 'list_capabilities.<locals>._list' was never awaited`
warnings, no SSE deltas after the user message, no eventual
`message.completed`.

Repro: HDF5 fixture in /tmp/clio-demo, `gact agent deploy clio`,
ask "Analyze /tmp/clio-demo/clio_demo.h5". Hangs.

Fix scope: investigate MCPToolBridge thread/loop interaction with
FastAPI's executor + the running uvicorn loop; surface tool errors
as `tool.call.completed{ok:false}` events instead of swallowing.

### 2. LM provider config is deploy-time only

`gact agent deploy clio my-clio` requires `CLIO_LM_*` to be set in
the parent shell. There's no in-TUI way to configure or change
provider/model. The frictionless deploy story breaks: a fresh user
gets a 503 on every POST until they read docs and re-deploy.

Fix scope:

- Server: GET / PUT `/v1/providers/lm` accepting
  `{provider, api_base, model, api_key}`. PUT triggers a soft
  rebuild of the LM + dependent experts (no process restart).
- TUI: on connect, if `/v1/health.integrations.agent.status ==
  "unavailable"` (or a new `lm_unconfigured` flag), open a modal
  asking for the four fields. Persist last value in
  `~/.config/gact/clio.json` so we don't ask twice.
- Provider picker offers OpenAI / Anthropic / OpenRouter / LM Studio /
  Ollama / Argonne ALCF presets out of the box plus a custom slot.

### 3. Tokens + cost stay zero with real Claude

`Prediction.tokens` and `.cost_usd` aren't populated by DSPy. We
need to read `dspy.LM.history` or the per-call `usage` after each
forward and feed the numbers to the GACT layer. Otherwise the
sidebar/footer chips and `/v1/metrics` claim zero usage every turn,
which matters because the user is paying-by-quota through the
upstream provider.

Fix scope: add a `_extract_usage(pred, lm)` helper that pulls from
DSPy's history, plumb through `app.state.agent.last_usage()` or
similar.

### 4. Streaming is only partially live

`message.part.delta` events can come from two different sources:

- `stream_source="live"`: text arrived through the live
  `dspy.streamify` path.
- `stream_source="synthetic_posthoc"`: the backend already had the
  final assistant text and chunked it afterward for TUI rendering
  continuity.

The chat `answer` path can stream live when the upstream DSPy/LiteLLM
provider supports it. Expert paths that do not expose the same `answer`
stream still fall back to post-hoc 64-character chunks. The TUI should
render both, but only `live` is evidence of real token arrival.

Fix scope: extend live streaming beyond the chat `answer` listener to
expert outputs, and keep the explicit `stream_source` marker so the UI
and tests can tell when a path regresses to synthetic chunks.

## Wire-shape-only — work but no real driver

These have correct wire shape + capability flags + endpoint
behaviour but the real ClioAgent doesn't drive them. They produce
zero events / no Parts / no rows during a real conversation; only
the smoke server's fake agent triggers them. Documenting honestly
so the tests prove what they prove.

| Capability | Endpoint works | Real agent emits | Notes |
|---|---|---|---|
| `subagents` | yes | no | ClioAgent has no Tier-3 spawn primitive yet |
| `diffs` | yes | no | No edit_file tool that produces diffs |
| `permissions` | yes | no | MCPToolBridge doesn't gate destructive ops |
| `cancellation` (best-effort) | yes | partial | Server settles the GACT envelope as cancelled; executor-thread provider/tool work may continue and is flagged with `execution_cancellation="best_effort"` |
| `tool_telemetry` events | yes | partial | Synthesised from `tools_called` post-hoc; not live |

## What does work end-to-end against real Claude

- POST messages → planner decision → explicit no-action explanation
  or chat path → text answer → `message.completed`
- `/v1/health.integrations[]` reflects real ClioAgent + ARC state
- Sessions CRUD, fork (in-memory copy), search (in-memory match)
- `/v1/memory/stats` from the real ARCMemory (cache hit rate updates)

## Closing the GitHub issues

Each iowarp/clio-agent issue (#2-#11) maps to one v0.2 capability.
We DO NOT close an issue until:

1. The capability flag is `true`.
2. An integration test in `tests/test_integration_v0_2/` drives the
   capability through `clio-agent-gact` against a real LM.
3. The test passes against a real Anthropic Claude turn AND a sanity
   check passes against an OpenRouter free model (proves no
   Claude-specific assumption sneaked in).

Status today: zero issues actually closeable. Wire shapes correct,
real-driver coverage partial.
