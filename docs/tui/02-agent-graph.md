# 02 — Agent Graph: The Message Loop

> How a single user turn traverses CLIO from input to output. Everything cited against `src/clio_agent/`.

## Single-turn call flow

Entry point: `ClioAgent.forward(question: str, session_id: str = "default") → dspy.Prediction` (`agent.py:247-383`).

```
USER INPUT (question, session_id)
  │
  ├─ 1. Retrieve session context from ARC memory          (agent.py:269)
  │       arc.get_session_context() via ContextRetriever
  │       returns compiled context (4K tokens for Tier-2)
  │       falls back to "No prior context" on error
  │
  ├─ 2. Route query                                       (agent.py:277-298)
  │       a) heuristics first (agent.py:386-402)
  │          · plots / visual* → "visualization"
  │          · .h5 / hdf5     → "data"
  │          · .parquet        → "analysis"
  │       b) DSPy router on miss (ChainOfThought, temp 0.3)
  │          signatures/main_agent_sig.py:14-50
  │          output: Literal["data"|"analysis"|"visualization"|"chat"|"none"]
  │
  ├─ 3. Load file context                                 (agent.py:300)
  │       arc.get_session_profiles() → DatasetProfile[]
  │
  ├─ 4. Dispatch to expert or chat                        (agent.py:302-352)
  │       "data"          → DataExpert.forward(question, file_context)
  │       "analysis"      → AnalysisExpert.forward(...)
  │       "visualization" → VisualizationExpert.forward(...)
  │       "chat"          → _run_chat_agent(question, session_context)
  │       "none"          → out-of-scope fallback
  │
  │       Expert inner flow (data_expert.py:82-98):
  │          a) _direct_tool_answer()  — fast path, no LM
  │          b) if none → dspy.ReAct loop
  │             · max 5 iterations (agent.py:310)
  │             · tools via MCPToolBridge.to_dspy_tools()
  │             · Thought → Action (MCP tool call) → Observation
  │
  ├─ 5. Store invocation                                  (agent.py:354-366)
  │       arc.store_invocation(Invocation(agent_id, tier=2,
  │         status, duration_ms, input, output, tools_called))
  │
  ├─ 6. Store conversation + routing + metrics            (agent.py:368-373)
  │       _store_conversation()      — append user+assistant messages
  │       _store_routing_decision()  — which expert selected + confidence
  │       _store_metrics()            — LSM-tree write
  │
  └→ RETURN dspy.Prediction(
        answer, selected_expert, duration_ms,
        arc_stats, lsm_stats, error_info
     )
```

## Observable events the TUI can surface

| Stage | TUI can display |
|---|---|
| Routing | Selected expert name + routing decision (heuristic vs LM) |
| Expert dispatch | "DataExpert is thinking…" header; which expert is active |
| Tool calls (inside ReAct) | Tool name, args, result length, **cached vs fresh** (from ARC stats) |
| Completion | `duration_ms`, `arc_stats["hit_rate"]`, `lsm_stats` |
| Errors | Structured `error_info` with `error_type` ∈ { routing_error, expert_error, tool_error, … } |

## Execution model

- **Synchronous** today — `forward()` blocks until the expert finishes.
- **No token streaming from the agent core.** SSE events (`routing`, `chunk`, `done`) exist but are synthesised in the FastAPI layer (`test_api.py:238-271`); they don't reflect real token arrival from the LM.
- **Not cancellable.** No cancel hooks in the ReAct loop or the dispatch path.
- Max 5 ReAct iterations per expert turn (`agent.py:310`, `data_expert.py:85`).

**Implication for gact-tui:** the TUI can still *render* streaming-style progress (expert active, tool running) based on the observable stages, but the final answer will arrive as a single blob. True token streaming needs upstream work in CLIO (Phase 4, REST async endpoints — see 09-integration-plan.md).

## Conversation & session model

- `session_id` is user-provided (`default` if omitted) — analogous to GACT's `/v1/sessions/{id}` but **not** server-issued.
- Conversations stored as `Conversation(session_id, user_id, created_at, updated_at, status, messages[], routing_decisions[], metadata)` in ARC (`arc/schema.py:91-126`).
- Messages are `{role, content, timestamp, message_id}`; `role ∈ {user, assistant}` only (no explicit `tool` or `system` role in the persisted model).
- Multi-turn: successive `forward()` calls with the same `session_id` append to the same `Conversation` record and retrieve its context.
- No server-side session *create/list/delete* endpoints yet — the TUI owns the `session_id` UUID.

## Routing table

| Heuristic trigger | Fallback LM |
|---|---|
| "plot", "visual*" → visualization | ChainOfThought router classifies into `data / analysis / visualization / chat / none` with temp 0.3 |
| `.h5`, `hdf5` → data | |
| `.parquet` → analysis | |

Both paths end up selecting an agent registered with `AgentCapability(keywords, description, tools, specialization)` via the Registry (`registry/registry.py:67-100+`).

## State-surface summary

```
agent.registry.list_agents()            → ["data", "analysis", "visualization"]
agent.registry.get_capabilities("data") → AgentCapability(keywords, tools, …)
agent.arc.get_conversation(session_id)  → Conversation | None
agent.arc.get_cache_stats()             → {hits, misses, hit_rate}
agent.arc.get_invocations_by_agent(id)  → [Invocation, …]
agent.arc.get_metrics(agent_id, period) → Metrics
```

These are the TUI's data feeds today (via REST endpoints or in-process Python — see `06-endpoints.md`).
