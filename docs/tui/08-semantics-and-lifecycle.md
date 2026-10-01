# 08 — Semantics & Lifecycle

> Behavioural guarantees extracted from `tests/`. These are the **authoritative** semantics — whatever the docs promise, the tests are what's actually pinned.

## Test surface at a glance

- **test_core/** — errors, config, instrumentation, runner
- **test_gact/test_clio_react.py** — the agent loop (`ClioReAct`)
- **test_gact/test_agent_blueprints.py** — registry bootstrap, Agent Blueprint parsing,
  DSPy module compilation, child expert tools, and native-expert removal guards
- **test_tools/** — FastMCP gateway + HDF5 / Parquet servers end-to-end
- **test_arc/** — memory coverage, context compiler, retrieval, storage tiers
- **integration-contract / benchmark evidence** — real provider and marketplace
  deployment checks; unit coverage is not sufficient for release readiness

Fixtures (`conftest.py:1-102`) create real files (HDF5 / Parquet) with deterministic seeds — CLIO uses **real file I/O** in its tests, no mocked backends. Only LM calls get mocked (or skipped with `@skipif(not lm_studio_available())`).

## Agent lifecycle

### A turn

A turn is `POST /v1/sessions/{sid}/messages`. The turn engine (`gact/turn.py`,
`gact/turn_forward.py`):

1. resolves the session's agent -- the activated Agent Blueprint's root expert, or
   the builtin `main` for a session with no blueprint (an explicit activation that
   resolves nothing is a typed `no_resolvable_agent` failure, never a substitute);
2. builds that expert's DSPy module by its declared `module.kind` (`react` experts
   run `ClioReAct`, `gact/agents/clio_react.py`; `predict` / `chain_of_thought`
   run the DSPy module of that name, optionally wrapped in a declared
   `BestOfN` / `Refine` variant);
3. runs the module's `forward` ONCE in the turn's forward executor;
4. finalizes the assistant message (parts, tokens, cost, `stop_reason`).

### The agent loop (`ClioReAct`)

The agent's context is its scope's ARC live plane, and it spans turns: every earlier
turn's user message, steps, tool results and answer are there as the messages they
were (never a prose recap), and nothing is wiped between turns -- only a recorded op
removes content (compaction summarizes it; undo / rewind roll it back to before the
first rolled-back turn). A turn first records CLIO's additions (plan reminder, todos,
replan suggestion, task notifications, memory hits) as user messages headed
`[clio: <source>]` -- each source again only when its text changed -- then the user's
message. An agent new to a conversation that already has turns is seeded once from
the transcript and told so (`earlier_turns`).

Each step: cancellation is checked; what arrived since the last step (a user steer, a
finished child's result) is recorded as its own user message; the context is folded
from the plane (`fold_steps`); one model call returns thinking, text and tool calls;
the step's tool calls run **concurrently** (results in call order); the step is
recorded (`react.step.completed`, ARC segments). Every note CLIO adds to a call's
result -- a path hint, the circuit breaker, a spilled oversize result, a hook's
effect -- is in the result the model reads and is shown to the user as an `injection`
part with the exact text. The loop ends when the model calls no tool
(the text is the answer, `termination_reason="direct_response"`), calls `submit`
(typed outputs), calls `ask_user` / `plan_exit` (the turn yields to the user),
or hits a declared `max_iters` / a parse error / the context window. Nothing
calls the model after the loop (`docs/design/react-loop-completion-2026-09.md`).
A terminal MCP protocol refusal escalates as a typed turn failure.

Delegation is the model's choice: a react expert with declared children spawns
them as real child sessions through `spawn_agent_task` /
`spawn_agents_parallel` and collects them with `wait_agent_tasks` /
`observe_agent_tasks`.

(`tests/test_gact/test_clio_react.py`, `tests/test_gact/test_agent_blueprints.py`)

### Cancellation

Cancellation is supported at the GACT boundary as **best effort** through
`POST /v1/sessions/{sid}/cancel`.

- Idle sessions move to `status="cancelled"` and emit `session.cancelled`.
- If a cancel lands before the turn produces output, the assistant message
  settles with `error_info.error="cancelled"` and no text body.
- If a cancel lands while the turn is running in an executor thread, the
  GACT envelope still settles as cancelled and includes
  `details.execution_cancellation="best_effort"`. Provider or tool work that
  is already running may continue after the envelope settles.
- If a cancel lands while the turn's off-loop prologue (transcript persist,
  enrichment, `UserPromptSubmit` hooks) is still running, it settles as
  `error_info.error="turn_cancelled_during_prologue"` instead — the prologue
  stops at its next cooperative checkpoint and kills an already-running hook
  subprocess outright.

Tests: `tests/test_gact/test_cancellation.py`.

### Streaming

`clio-agent serve` streams GACT events over `GET /v1/sessions/{sid}/events`. A user turn is accepted with `POST /v1/sessions/{sid}/messages`, then the SSE channel emits `message.created`, `message.part.added`, `message.part.delta`, `message.part.completed`, and `message.completed`.

Text streaming has explicit provenance:

- `stream_source="live"` means the delta arrived live from the provider through the LM token hooks (`runtime/lm_activity`).
- `stream_source="batch"` means the backend already had the final answer before live provider-token deltas could be emitted.

Batch fallback payloads include a structured `stream_fallback`
object with `reason`, `category`, `description`, `recovery_actions`,
legacy `synthetic_posthoc=true`, and `live_streaming=false`. The audited
reason catalog is advertised in `/v1/capabilities`; unknown reasons are
rejected instead of becoming unclassified fallback metadata.

Every model call in the loop streams its text and thinking live when the provider streams; a turn whose answer never produced a live delta is marked `batch` (`stream_fallback.reason="sync_execution_path"`) and delivered as a completed part, not fake deltas. A provider failure surfaces as a structured `provider_error`; nothing re-runs the agent. `details.partial_output` tells clients whether any live text was already emitted.

Tests: `tests/test_gact/test_streaming.py`.

The legacy `clio-agent-api` `/query` endpoint has been **removed** (its
console script is now a deprecation shim). Use native GACT events
(`/v1/sessions/{sid}/events`) for best-effort live streaming.

## Error semantics

`errors.py:26-126` defines the hierarchy:

```
ClioError (base)
 ├── ProviderError   — LM unavailable / timeout
 ├── RoutingError    — a routing/selection request could not be satisfied
 ├── ExpertError     — an expert's loop failed
 ├── ToolError       — MCP tool call failed
 └── ConfigError     — env / config invalid
```

### Structured error response

```python
err.to_dict()
# {
#   "error": "expert_error",
#   "message": "Human-readable...",
#   "details": {"expert": "data", "original_error": "..."}
# }
```

`format_error_response(exc)` (errors.py:107-126) maps arbitrary exceptions to the same shape **without** leaking tracebacks:

```python
if isinstance(err, ClioError): return err.to_dict()
else: return {"error": "internal_error",
              "message": "An internal error occurred",
              "details": {}}
```

Use this on the TUI side: `error_info["error"]` is the machine tag; `error_info["message"]` the user-facing line; `details` optional context.

## Permission Semantics

`permissions=true` means CLIO exposes the GACT permission surface and
uses it before destructive mutations, not only after the fact.

- Destructive MCP tool calls create `permission.requested` rows unless a
  stored policy allows or denies first.
- Session `mode="plan"` and `mode="architect"` auto-deny destructive
  tool calls.
- `/diffs/apply` records an auto-approved permission audit row because
  the user explicitly clicked apply.
- Direct destructive GACT DELETE endpoints (`sessions`, `messages`,
  context file attachments, tasks, schedules, agents, workspaces, hooks,
  and external MCP server registrations) consult permission policies
  before mutation and record resolved audit rows.

### Failure surfacing

CLIO does not use fallback value substitution for agent or provider failures.
Failed routes and failed LM calls surface as structured `error_info` with retry,
reconfigure-provider, and exit recovery actions.

- An agent that cannot be resolved → structured `no_resolvable_agent` /
  `blueprint_root_disabled`, never a substitute agent.
- Provider/LM failure → structured `provider_error`; CLIO must not hide
  an upstream/provider failure behind repeated, canned, or locally synthesized
  assistant text.
- Tool failure → return `{"error": {...}}` dict, not raise (see [`../MCP_TOOL_INTEGRATION.md`](../MCP_TOOL_INTEGRATION.md)).

(`test_errors.py`, `tests/test_gact/test_stream_failures.py`)

## Storage & persistence semantics

### Invocation record per expert turn

```python
Invocation(
    trace_id="...",
    session_id="...",
    agent_id="data",        # expert id
    tier=2,                 # 2 = Expert, 1 = Main, 3 = Nanoagent
    status="success" | "failure" | "timeout",
    duration_ms=1234.5,
    input={"question": "..."},
    output={"analysis": "...", "recommendations": "..."}
      # or {"error": "..."} on failure
    tools_called=[ToolCall, ...],
    nanoagents_spawned=[],
)
```

(`test_memory_coverage.py:35-54`)

### Cache + disk fallback

```python
arc.store_invocation(inv)
arc.get_invocation("trace-1")   # Cache hit
arc.clear_cache()
arc.get_invocation("trace-1")   # Still returns — tier-2 storage fallback
```

(`test_memory_coverage.py:78-101`)

### Metrics with period query

```python
arc.store_metrics(Metrics(
    agent_id="data",
    period="2025-01",
    invocations=InvocationStats(total=100, success=95),
    latency=LatencyStats(mean=1500.0, p50=1200.0, p99=8000.0),
))
arc.get_metrics("data", period="2025-01")
arc.get_metrics("data")         # latest
```

(`test_memory_coverage.py:122-150`)

## Routing semantics

- There is no planner. The session's active expert runs its own loop; routing to
  other experts is the model calling the spawn tools for its declared children.
- Session `routing_mode` is recorded metadata only; no code path branches on it.

## Expert semantics

- Agent Blueprint experts compile by declared `module.kind`, not by tool list.
- Empty blueprint signatures default to `system_prompt`, `question`, and `answer`; structured outputs add their declared fields, which ride the `submit` tool.
- ReAct blueprint experts receive their declared tools, the spawn tools for their declared children, `load_skill` when they have skills, and the auto-attached `create_artifact` / `plan_exit` / `write_todos`.
- A child runs as a real child session; its result returns to the parent through `wait_agent_tasks`.
- Native Python domain expert modules are not runtime-importable and must not be used as a fallback.

## Tool semantics

- Tools validate paths **before** opening files (`test_hdf5_server.py`) — a `file_policy` error surfaces as a structured dict.
- Tools return error dicts, not raise exceptions; an exception a tool does raise becomes that call's error observation, and the loop continues.
- Tool namespacing is stable: a declared MCP server `x` exposes `x_<tool>`.
- Calls run concurrently, including calls to the same MCP server (no per-executor call lock).

## SIMBA optimiser (offline tuning)

`optimizer/runner.py` runs DSPy SIMBA with statistical significance gating:

```python
result = runner.run(
    module=MockModule(),
    agent_id="data",
    trainset=[dspy.Example(...)] * 10,  # min 5 examples
    metric_fn=custom_metric,
)
# result = {
#   "optimized": dspy.Module,
#   "before_score": 60.0, "after_score": 85.0,
#   "improvement_delta": 25.0, "p_value": 0.001,
#   "is_significant": True,
#   "variant_record": VariantRecord,
#   "train_size": 2, "val_size": 8,
# }
```

`test_significance()` runs a proportion z-test on before/after success rates; only stat-sig variants get deployed. Variants are versioned in ARC; the TUI (or admin) can roll them back.

(`test_runner.py:56-218`)

## Copy-paste minimal end-to-end

```bash
$ clio-agent serve --host 127.0.0.1 --port 8100 &
$ SID=$(curl -s -X POST http://127.0.0.1:8100/v1/sessions -H 'Content-Type: application/json' \
    -d '{"title":"t"}' | jq -r .id)
$ curl -s -X POST http://127.0.0.1:8100/v1/sessions/$SID/messages \
    -H 'Content-Type: application/json' -d '{"parts":[{"type":"text","text":"Hi"}]}'
$ curl -s http://127.0.0.1:8100/v1/sessions/$SID/messages   # newest first
```

## Integration-point summary

| What TUI needs | Authoritative pin |
|---|---|
| Turn | `tests/test_gact/test_post_messages.py` — `POST /v1/sessions/{sid}/messages` |
| Agent loop | `tests/test_gact/test_clio_react.py` — ClioReAct steps, concurrency, termination |
| Streaming | `tests/test_gact/test_streaming.py` — live deltas via the LM token hooks |
| Errors | `test_errors.py` — structured `to_dict()`, no traceback leak |
| ARC memory | `test_memory_coverage.py:35-150` — Invocation + Metrics schemas pinned |
| Tools | `test_hdf5_server.py:37-120` — file_policy validation BEFORE open, error dict on failure |
| Registry | `test_registry.py` — registry-backed Agent Blueprint experts |
| Variants | `test_runner.py:141-218` — SIMBA + stat-sig gating + VariantRecord in ARC |
