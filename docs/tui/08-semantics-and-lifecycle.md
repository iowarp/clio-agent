# 08 — Semantics & Lifecycle

> Behavioural guarantees extracted from `tests/`. These are the **authoritative** semantics — whatever the docs promise, the tests are what's actually pinned.

## Test surface at a glance

- **test_core/** — agent loop, routing, errors, config, instrumentation, runner, API
- **test_experts/** — DataExpert, AnalysisExpert, VisualizationExpert (tool wiring + signature shape)
- **test_tools/** — FastMCP gateway + HDF5 / Parquet servers end-to-end
- **test_arc/** — memory coverage, context compiler, retrieval, storage tiers
- **test_integration/** — full `ClioAgent` wiring + multi-expert dispatch

Fixtures (`conftest.py:1-102`) create real files (HDF5 / Parquet) with deterministic seeds — CLIO uses **real file I/O** in its tests, no mocked backends. Only LM calls get mocked (or skipped with `@skipif(not lm_studio_available())`).

## Agent lifecycle

### Construction

```python
from clio_agent import ClioAgent
agent = ClioAgent(data_dir=str(tmp / "clio_test"), verbose=True)
# internally: setup_dspy(), instantiate experts, register with registry,
# wire ARC + LSM, load active variants (optimizer/variants), mount tools.
```

(`test_agent.py:15-52`, `test_end_to_end.py:34-48`)

Shape after init:

```python
agent.action_planner                  # DSPy ChainOfThought on AgentActionSignature
agent.chat_agent                      # DSPy Predict on ChatAgentSignature
agent.data_expert                     # DataExpert (DSPy ReAct)
agent.analysis_expert                 # AnalysisExpert
agent.visualization_expert            # VisualizationExpert
agent.arc                             # ARCMemory
agent.lsm                             # LSM tree for metrics
agent.registry.get_agent_count() == 3 # data / analysis / visualization
```

### Single turn (happy path)

```python
result = agent.forward(question="Hello", session_id="conv_test")
# or agent(question=..., session_id=...) via __call__
```

Returns `dspy.Prediction` with:

- `answer: str`
- `selected_expert: Literal["data","analysis","visualization","chat","none"]`
- `session_id: str`
- `duration_ms: float`
- `arc_stats: dict` (cache hits/misses)
- `lsm_stats: dict`
- `error_info: dict | None`

`agent.arc.get_conversation(session_id).messages` has `len == 2 * turn_count` (user + assistant).

(`test_agent_dispatch.py:26-252`, `test_end_to_end.py:59-71`)

### Multi-turn conversation semantics

- Successive `forward()` calls with the same `session_id` **append** to the existing `Conversation`.
- Context retrieval (`arc.get_session_context`) injects prior messages into the LM prompt at turn start.
- **Context window**: Tier-1 (Main Agent) 2 K tokens, Tier-2 (Expert) 4 K tokens. Overflow → compaction (oldest first).
- Routing decisions are persisted per turn; `Conversation.routing_decisions` grows to match turn count.

(`test_end_to_end.py:202-215`, `test_context_compiler.py:24-109`)

### Cancellation

Cancellation is supported at the GACT boundary as **best effort** through
`POST /v1/sessions/{sid}/cancel`.

- Idle sessions move to `status="cancelled"` and emit `session.cancelled`.
- If a cancel lands before the turn produces output, the assistant message
  settles with `error_info.error="cancelled"` and no text body.
- If a cancel lands while the turn is running in an executor thread, the
  GACT envelope still settles as cancelled and includes
  `details.execution_cancellation="best_effort"`.
- Provider or tool work that is already running may continue after the
  envelope settles. The status event exposes
  `executor_work_may_continue=true` for this case.

Tests: `tests/test_gact/test_cancellation.py`.

### Streaming

`clio-agent-gact` streams GACT events over `GET /v1/sessions/{sid}/events`. A user turn is accepted with `POST /v1/sessions/{sid}/messages`, then the SSE channel emits `message.created`, `message.part.added`, `message.part.delta`, `message.part.completed`, and `message.completed`.

Text streaming has explicit provenance:

- `stream_source="live"` means the delta arrived through the live `dspy.streamify` path.
- `stream_source="synthetic_posthoc"` means the backend already had the final answer and chunked it after completion for TUI rendering continuity.

Current limitation: the chat `answer` path can stream live when the upstream DSPy/LiteLLM path emits chunks. Expert outputs and paths that do not emit an `answer` stream still fall back to synthetic post-hoc chunks. Mid-stream failures after user-visible output surface as structured `provider_error` messages with `details.partial_output=true`, not as a hidden sync rerun.

Tests: `tests/test_gact/test_streaming.py`.

The legacy `clio-agent-api` `/query` endpoint still has its own SSE shape (`routing`, `chunk`, `done`) and those chunks are composed from the final answer. That is legacy API behavior, not the native GACT TUI contract.

## Error semantics

`errors.py:26-126` defines the hierarchy:

```
ClioError (base)
 ├── ProviderError   — LM unavailable / timeout
 ├── RoutingError    — planner could not select or validate a safe action
 ├── ExpertError     — expert ReAct loop blew up
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

### Failure surfacing

CLIO does not use fallback value substitution for planner or provider failures.
Failed routes and failed LM calls surface as structured `error_info` with retry,
reconfigure-provider, and exit recovery actions.

- Planner route failure → structured `routing_error`, not a canned answer.
- Provider/LM failure → structured `provider_error`; CLIO must not hide
  an upstream/provider failure behind repeated, canned, or locally synthesized
  assistant text.
- Tool failure → return `{"error": {...}}` dict, not raise (see `05-tools.md`).

(`test_errors.py`, `test_agent_dispatch.py`, `agent.py`)

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

- `AgentActionSignature` returns a constrained JSON action. Valid action kinds are `tool`, `expert`, `answer`, and `none`.
- `expert` actions select one of `data`, `analysis`, or `visualization`; those are the three registry-backed experts, so `registry.get_agent_count() == 3`.
- `answer` is the conversational/chat path. It produces normal assistant text without tool execution.
- `none` means no valid action is available. A missing planner explanation surfaces as `routing_error`; CLIO must not replace it with a canned assistant response.
- Session `routing_mode` overrides constrain the planner path: `chat` forces the conversational path, `experts` rejects direct `answer`/`none` routes, and `reasoning_only` asks the planner to prefer tool/expert reasoning over deterministic shortcuts.
- Registry lookup by keyword remains available for discovery, for example `registry.find_agents_by_keyword("hdf5")` returns the data expert.

## Expert semantics

- `DataExpert._tools` populated from `MCPToolBridge(gateway).to_dspy_tools()` at construction — at least 4 tools expected (`test_data_expert.py:79-93`).
- Each expert's Signature has a ≥ 500-word docstring that drives LM behaviour (`CLAUDE.md` Rule 4). Don't display this in the TUI.
- Experts accept a `tool_executor` parameter for testability (`FakeExecutor` with `.to_dspy_tools()`) — useful if the TUI wants to mock out tools for dry-runs.
- Experts close their executor on `shutdown()` (`test_data_expert.py:142-144`).

## Tool semantics

- Tools validate paths **before** opening files (`test_hdf5_server.py:37-80`) — `file_policy` error surfaces as a structured dict, h5py is never touched.
- Tools return error dicts, not raise exceptions (`test_hdf5_server.py:142-150`).
- Tool namespacing is stable across FastMCP versions — `hdf5_*` prefix holds (`test_gateway.py:71-87`).
- Exactly 8 tools today: 5 `hdf5_*` + 3 `parquet_*` (`test_gateway.py:96`).

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

(`test_runner.py:56-218`, `SELF_IMPROVEMENT.md`)

## Copy-paste minimal end-to-end

```python
from clio_agent import ClioAgent

agent = ClioAgent()                              # uses $HOME/.clio_agent by default
result = agent(question="Hi", session_id="abc")
print(result.answer)                             # final answer
print(result.selected_expert)                    # "chat" | "data" | ...
print(result.duration_ms)

conv = agent.arc.get_conversation("abc")
print(len(conv.messages))                        # 2 (user + assistant)

agent.shutdown()                                 # flush ARC + close LSM
```

For a server-mode equivalent:

```bash
$ clio-agent-api --host 127.0.0.1 --port 8000 &
$ curl -s -X POST http://127.0.0.1:8000/query \
    -H 'Content-Type: application/json' \
    -d '{"question":"Hi","session_id":"abc"}'
# → {"answer":"...","selected_expert":"chat","session_id":"abc","duration_ms":...,"error_info":null}
```

## Integration-point summary

| What TUI needs | Authoritative pin |
|---|---|
| Agent init | `test_agent.py:15-52` — `ClioAgent(data_dir?, verbose?)` |
| Per-message call | `test_agent_dispatch.py:26-44` — `forward(question, session_id?) → Prediction(...)` |
| Conversation | `test_end_to_end.py:59-71` — `arc.get_conversation(sid).messages` |
| Streaming | `test_api.py:238-271` — SSE composed in FastAPI, not token-native |
| Errors | `test_errors.py` — structured `to_dict()`, no traceback leak |
| ARC memory | `test_memory_coverage.py:35-150` — Invocation + Metrics schemas pinned |
| Context window | `test_context_compiler.py:24-109` — 2K (T1) / 4K (T2) budgets |
| Tools | `test_hdf5_server.py:37-120` — file_policy validation BEFORE open, error dict on failure |
| Registry | `test_registry.py` — 3 registry-backed experts plus planner-selected chat/none outcomes |
| Variants | `test_runner.py:141-218` — SIMBA + stat-sig gating + VariantRecord in ARC |
