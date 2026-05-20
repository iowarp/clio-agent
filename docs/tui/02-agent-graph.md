# 02 - Agent Graph: The Message Loop

> How a single user turn traverses CLIO from input to output. Everything cited against `src/clio_agent/`.

## Single-Turn Call Flow

Entry point: `ClioAgent.forward(question: str, session_id: str = "default", *, session_mode: str = "chat", session_edit_mode: str = "diff") -> dspy.Prediction`.

```text
USER INPUT (question, session_id)
  |
  |-- 1. Retrieve session context from ARC memory
  |       arc.get_session_context() via ContextRetriever
  |       returns compiled context for the active session
  |
  |-- 2. Resolve current file context
  |       explicit file paths in the question win
  |       otherwise CLIO reuses the most recent scientific file in session history
  |
  |-- 3. Plan next action
  |       DSPy planner returns exactly one JSON action:
  |         - {"action":"tool", ...}
  |         - {"action":"expert", "expert":"data|analysis|visualization", ...}
  |         - {"action":"answer", ...}
  |         - {"action":"none", ...}
  |
  |-- 4. Execute selected action
  |       "tool"   -> direct tool call through the MCP gateway
  |       "expert" -> Data / Analysis / Visualization expert
  |       "answer" -> chat answer via ChatAgentSignature
  |       "none"   -> planner explanation, or routing_error
  |
  |       Expert inner flow:
  |         - DataExpert / AnalysisExpert use DSPy ReAct with MCP tools.
  |         - VisualizationExpert uses registered plotting tools.
  |         - Tool results are surfaced through trace/tool metadata.
  |
  |-- 5. Store invocation, conversation, routing decision, and metrics
  |
  `-- RETURN dspy.Prediction(
        answer, selected_expert, route_source, route_reason,
        duration_ms, arc_stats, lsm_stats, error_info
      )
```

## Observable Events The TUI Can Surface

| Stage | TUI can display |
|---|---|
| Routing | Selected action/expert plus planner rationale |
| Expert dispatch | Active expert header and selected capability |
| Tool calls | Tool name, args summary, result length, cached/fresh state |
| Completion | `duration_ms`, ARC stats, LSM stats, token/cost metadata |
| Errors | Structured `error_info` with retry/reconfigure/exit recovery actions where applicable |

## Execution Model

- **Synchronous core.** `forward()` blocks until the agent loop finishes.
- **GACT may stream text live when DSPy/LiteLLM exposes listener chunks.** Expert paths that do not stream live are marked as `synthetic_posthoc`.
- **Cancellation is best-effort at the GACT boundary.** If provider/tool work is already running in an executor thread, the backend reports that upstream work may continue.
- The planner has a bounded step limit; when it cannot complete cleanly, CLIO surfaces structured routing/tool/provider errors instead of hiding the failure behind canned text.

## Conversation & Session Model

- `session_id` is user-provided (`default` if omitted) and maps to GACT session IDs at the HTTP boundary.
- Conversations are stored as `Conversation(session_id, user_id, created_at, updated_at, status, messages[], routing_decisions[], metadata)` in ARC.
- Messages are `{role, content, timestamp, message_id}`; persisted roles are user/assistant.
- Multi-turn calls with the same `session_id` append to the same conversation record and retrieve that session context.
- GACT session metadata (`mode`, `edit_mode`, `routing_mode`) is passed through the adapter layer; write enforcement and diff shaping remain GACT responsibilities.

## Planner Action Table

| Planner action | Runtime behavior |
|---|---|
| `tool` | Calls a listed MCP/visualization tool and records the observation. |
| `expert` | Delegates to `data`, `analysis`, or `visualization` after file compatibility checks. |
| `answer` | Runs the chat answer path for conversation, capability, or safety-style responses. |
| `none` | Requires a meaningful planner explanation; missing explanations surface as `routing_error`. |

The planner sees registered expert/tool capabilities built from the Registry (`registry/registry.py`) and the live MCP tool gateway.

## State-Surface Summary

```text
agent.registry.list_agents()            -> ["data", "analysis", "visualization"]
agent.registry.get_capabilities("data") -> AgentCapability(keywords, tools, ...)
agent.arc.get_conversation(session_id)  -> Conversation | None
agent.arc.get_cache_stats()             -> {hits, misses, hit_rate}
agent.arc.get_invocations_by_agent(id)  -> [Invocation, ...]
agent.arc.get_metrics(agent_id, period) -> Metrics
```

These are the TUI's data feeds today via REST endpoints and message metadata.
