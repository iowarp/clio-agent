# Real-readiness gap log

Honest list of what's wire-shape-only vs end-to-end-working when CLIO
runs against a real configured LM provider, including direct cloud
providers and local OpenAI-compatible providers such as LM Studio.
Drives what to fix before declaring v0.2 ready.

Updated as gaps land or close.

## Hard blockers - must fix before "ready"

None currently tracked in this file.

## Fixed / no longer hard blockers

These used to be hard blockers in this file, but the current GACT
surface has runtime support and tests. Keep watching real-provider
regressions, but don't treat these as unresolved release blockers:

| Area | Current status | Evidence |
|---|---|---|
| Streaming provenance | Chat answers and provider-backed expert synthesis emit live `message.part.delta` events when DSPy/LiteLLM streams. Deterministic tool-result summaries can still emit `stream_source="synthetic_posthoc"` because no provider tokens exist to stream; the wire marker remains explicit. | `tests/test_gact/test_streaming.py`, real LM Studio/Qwopus data-expert synthesis smoke |
| LM provider config | `GET / PUT /v1/providers/lm` lets the TUI configure or hot-swap provider/model without redeploying the GACT process. | `tests/test_gact/test_lm_provider.py` |
| Tokens + cost | Per-turn tokens/cost populate assistant messages, completion events, session rollups, and `/v1/metrics`; GACT also extracts DSPy history/usage and estimates known-model cost when upstream omits cost. | `tests/test_gact/test_cost_tracking.py`, `tests/test_gact/test_cost_estimate.py` |
| DataExpert tool execution | Real GACT data turns complete instead of hanging, and native tool traces are exposed as `tools_called` metadata. | `tests/test_integration/test_local_filesystem_smoke.py`, `tests/test_gact/test_tools_called.py`, real LM Studio/Qwopus HDF5 smoke |
| Diff file edits | Real planner `fs_propose_edit` calls produce `file_diff` Parts with `new_content`; `/diffs/apply` writes accepted edits through the shared file-policy path. | `tests/test_core/test_agent_planner.py::test_forward_promotes_propose_edit_observation_to_file_diffs`, `tests/test_gact/test_plan_edit_modes.py::test_real_agent_propose_edit_trace_becomes_applicable_diff` |
| Prompt-only custom agents | Sessions selecting registered user or skill agents with no declared tools execute through DSPy/LiteLLM using the stored prompt and optional provider/model fields. | `tests/test_gact/test_post_messages.py::test_post_message_prompt_user_agent_executes_registered_agent` |
| Tool-declaring custom agents | Sessions selecting registered user/skill/extracted agents with declared tools execute through DSPy ReAct with the tool list restricted to the agent definition; unavailable declared tools surface as structured errors. | `tests/test_gact/test_post_messages.py::test_post_message_tool_user_agent_executes_registered_agent`, `tests/test_gact/test_post_messages.py::test_post_message_tool_user_agent_missing_declared_tool_sets_error_turn` |
| Subagents / nanoagents | Analysis expert nanoagent spawns are propagated by real `ClioAgent` predictions, materialized by GACT as child sessions/events, and retained in ARC invocation records. | `tests/test_core/test_agent_dispatch.py::TestForwardDispatch::test_dispatch_analysis_expert_propagates_nanoagent_spawns`, `tests/test_gact/test_nanoagents.py` |

## Streaming provenance

`message.part.delta` events can come from two different sources:

- `stream_source="live"`: text arrived through the live
  `dspy.streamify` path.
- `stream_source="synthetic_posthoc"`: the backend already had the
  final assistant text and chunked it afterward for TUI rendering
  continuity.

Synthetic text part events and assistant completion metadata also carry
`stream_fallback.reason`. Render that reason when useful; do not present
synthetic chunks as evidence that the provider streamed live tokens.
Known reasons include `agent_not_streamable` for non-DSPy test/runtime
agents and `stream_setup_failed` for DSPy listener setup failures.
Registered user/skill agents, including tool-declaring agents backed by
DSPy ReAct, attempt the live `dspy.streamify` path first and only use
synthetic post-hoc chunks when streaming cannot start.

The TUI should render both sources, but only `live` is evidence of real
token arrival. Treat synthetic chunks as a truthful compatibility path,
not as proof that the upstream provider streamed.

## Wire-shape-only or partial runtime gaps

These have correct wire shape + capability flags + endpoint behaviour,
but the real `ClioAgent` either does not drive them yet or only drives
part of the runtime surface. Documenting honestly so the tests prove
what they prove.

| Capability | Endpoint works | Real agent emits | Notes |
|---|---|---|---|
| `permissions` | yes | partial | Native MCP executor calls gate destructive tool names, and `/diffs/apply` enforces stored deny/allow policies before writing. Remaining gap tracked in #218: inventory any non-tool destructive API paths that should share the same policy semantics. |
| `cancellation` (best-effort) | yes | partial | Server settles the GACT envelope as cancelled; executor-thread provider/tool work may continue and is flagged with `execution_cancellation="best_effort"` |
| `tool_telemetry` events | yes | partial | Native MCP executor calls emit live `tool.call.started/completed` without duplicate post-turn lifecycle events; paths that only expose `tools_called` after the turn are still rendered post-hoc |

## What does work end-to-end against real LM providers

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
3. The test passes against at least one explicitly configured real LM
   provider. Cross-provider sanity should be run when credentials or a
   local provider are available, but the tests must not embed provider
   credentials.

Status today: the hard blockers listed above are clearable only when
their endpoint behavior, real-agent driver, and provenance evidence all
match this document and `docs/CAPABILITIES_MATRIX.md`.
