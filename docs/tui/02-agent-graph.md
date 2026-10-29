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
  |-- 3. Plan next action or resolve selected session agent
  |       Core CLIO can still plan tool/answer actions.
  |       GACT sessions run their pinned plugin agent instead (its flow,
  |       `react` by default; see docs/PLUGINS_AND_FLOWS.md).
  |
  |-- 4. Execute selected action
  |       "tool"      -> direct tool call through the MCP gateway
  |       "answer"    -> chat answer via ChatAgentSignature
  |       "none"      -> planner explanation, or routing_error
  |
  |       GACT plugin agent inner flow:
  |         - the agent's flow runs; `react` drives ClioReAct over the
  |           tools, servers and skills its plugins bring
  |         - subagents and flows start as child tasks (spawn tools,
  |           run_flow / run_plan), returned through the mailbox
  |
  |-- 5. Store invocation, conversation, routing decision, and metrics
  |
  `-- RETURN dspy.Prediction(
        answer, selected_expert, route_source, route_reason,
        duration_ms, arc_stats, error_info
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
- **GACT may stream text live when DSPy/LiteLLM exposes listener chunks.** Expert paths that do not stream live are marked as `batch`.
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
| `answer` | Runs the chat answer path for conversation, capability, or safety-style responses. |
| `none` | Requires a meaningful planner explanation; missing explanations surface as `routing_error`. |

The planner sees registered expert/tool capabilities built from the Registry (`registry/registry.py`) and the live MCP tool gateway.

## State-Surface Summary

```text
GET /v1/agents                           -> installed agents and user agents
GET /v1/sessions/{sid}/agent             -> the session's pinned plugin resolution
agent.registry.list_agents()             -> core/runtime registry rows
agent.arc.get_conversation(session_id)  -> Conversation | None
agent.arc.get_cache_stats()             -> {hits, misses, hit_rate}
agent.arc.get_invocations_by_agent(id)  -> [Invocation, ...]
agent.arc.get_metrics(agent_id, period) -> Metrics
```

These are the TUI's data feeds today via REST endpoints and message metadata.
