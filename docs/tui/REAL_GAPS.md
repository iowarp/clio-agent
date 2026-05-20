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

### 2. Streaming is only partially live

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

## Fixed / no longer hard blockers

These used to be hard blockers in this file, but the current GACT
surface has runtime support and tests. Keep watching real-provider
regressions, but don't treat these as unresolved release blockers:

| Area | Current status | Evidence |
|---|---|---|
| LM provider config | `GET / PUT /v1/providers/lm` lets the TUI configure or hot-swap provider/model without redeploying the GACT process. | `tests/test_gact/test_lm_provider.py` |
| Tokens + cost | Per-turn tokens/cost populate assistant messages, completion events, session rollups, and `/v1/metrics`; GACT also extracts DSPy history/usage and estimates known-model cost when upstream omits cost. | `tests/test_gact/test_cost_tracking.py`, `tests/test_gact/test_cost_estimate.py` |

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
| `user` / `skill` / `extracted` agents | yes | no | `/v1/agents` surfaces definitions, prompts, tools, provider, and model metadata; ClioAgent's planner still routes only built-in experts/tools |

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
