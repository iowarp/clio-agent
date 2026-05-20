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
| Streaming provenance | Chat answers and provider-backed expert synthesis attempt live `message.part.delta` events when DSPy/LiteLLM streams. Deterministic or fallback text can still be marked `stream_source="synthetic_posthoc"` because no provider tokens were available to stream, but it is delivered as a completed part with a structured fallback reason rather than synthetic deltas. | `tests/test_gact/test_streaming.py`, real LM Studio/Qwopus data-expert smoke with `stream_completed_without_chunks` provenance |
| LM provider config | `GET / PUT /v1/providers/lm` lets the TUI configure or hot-swap provider/model without redeploying the GACT process. | `tests/test_gact/test_lm_provider.py` |
| Tokens + cost | Per-turn tokens/cost populate assistant messages, completion events, session rollups, and `/v1/metrics`; GACT also extracts DSPy history/usage and estimates known-model cost when upstream omits cost. | `tests/test_gact/test_cost_tracking.py`, `tests/test_gact/test_cost_estimate.py` |
| DataExpert tool execution | Real GACT data turns complete instead of hanging, and native tool traces are exposed as `tools_called` metadata. | `tests/test_integration/test_local_filesystem_smoke.py`, `tests/test_gact/test_tools_called.py`, real LM Studio/Qwopus HDF5 smoke |
| Tool telemetry events | Real tool execution boundaries emit `tool.call.started/completed` with `telemetry_source="live_observer"`. Post-turn `tools_called` traces remain metadata summaries and are not reconstructed into fake lifecycle events. | `tests/test_gact/test_tool_telemetry.py`, `tests/test_integration_v0_2/test_real_capabilities.py::test_real_tool_call_events_fire_during_turn` |
| Diff file edits | Real planner `fs_propose_edit` calls produce `file_diff` Parts with `new_content`; `/diffs/apply` writes accepted edits through the shared file-policy path. | `tests/test_core/test_agent_planner.py::test_forward_promotes_propose_edit_observation_to_file_diffs`, `tests/test_gact/test_plan_edit_modes.py::test_real_agent_propose_edit_trace_becomes_applicable_diff` |
| Permissions | Destructive MCP executor calls, direct third-party MCP calls, `/diffs/apply`, and direct destructive GACT DELETE endpoints enforce stored permission policies before mutation and record resolved audit rows. | `tests/test_gact/test_permission_gate.py`, `tests/test_gact/test_plan_edit_modes.py`, `tests/test_gact/test_message_delete.py` |
| Prompt-only custom agents | Sessions selecting registered user or skill agents with no declared tools execute through DSPy/LiteLLM using the stored prompt and optional provider/model fields. | `tests/test_gact/test_post_messages.py::test_post_message_prompt_user_agent_executes_registered_agent` |
| Tool-declaring custom agents | Sessions selecting registered user/skill/extracted agents with declared tools execute through DSPy ReAct with the tool list restricted to the agent definition; unavailable declared tools surface as structured errors. | `tests/test_gact/test_post_messages.py::test_post_message_tool_user_agent_executes_registered_agent`, `tests/test_gact/test_post_messages.py::test_post_message_tool_user_agent_missing_declared_tool_sets_error_turn` |
| Subagents / nanoagents | Analysis expert nanoagent spawns are propagated by real `ClioAgent` predictions, materialized by GACT as child sessions/events, and retained in ARC invocation records. | `tests/test_core/test_agent_dispatch.py::TestForwardDispatch::test_dispatch_analysis_expert_propagates_nanoagent_spawns`, `tests/test_gact/test_nanoagents.py` |

## Streaming provenance

`message.part.delta` events can come from two different sources:

- `stream_source="live"`: text arrived through the live
  `dspy.streamify` path.
- `stream_source="synthetic_posthoc"`: the backend already had the
  final assistant text before it could emit live provider-token deltas.

Synthetic text part events and assistant completion metadata also carry
`stream_fallback.reason`, `category`, `description`, `recovery_actions`,
`synthetic_posthoc=true`, and `live_streaming=false`. Render that reason
when useful; do not present synthetic post-hoc text as evidence that the
provider streamed live tokens. The complete audited reason catalog is
available from `/v1/capabilities.capabilities.x_clio_stream_fallback_reasons`;
unknown reasons are rejected so new downgrade paths cannot appear as
unclassified fallback metadata.
Known reasons include `agent_not_streamable` for non-DSPy test/runtime
agents, `stream_setup_failed` for DSPy listener setup failures, and
`stream_completed_without_chunks` when DSPy streaming produced a final
prediction but no user-visible token chunks.
Registered user/skill agents, including tool-declaring agents backed by
DSPy ReAct, attempt the live `dspy.streamify` path first and only label
completed text as synthetic post-hoc when streaming cannot start.
If streaming starts executing the agent and fails before or after visible
output, GACT surfaces a structured `provider_error` turn instead of
rerunning the sync path and returning synthetic answer text.

The TUI should render both sources, but only `live` is evidence of real
token arrival. Treat synthetic post-hoc text as truthful fallback
delivery, not as proof that the upstream provider streamed.

## Wire-shape-only or partial runtime gaps

These have correct wire shape + capability flags + endpoint behaviour,
but the real `ClioAgent` either does not drive them yet or only drives
part of the runtime surface. Documenting honestly so the tests prove
what they prove.

| Capability | Endpoint works | Real agent emits | Notes |
|---|---|---|---|
| `cancellation` (best-effort) | yes | partial | Server settles the GACT envelope as cancelled; compatible agents and the sync MCP bridge observe cooperative cancellation between execution boundaries. Late tool completions after cancellation are marked as cancellation/error telemetry and are not carried into later turn metadata. Already-running provider/tool work may still continue and is flagged with `execution_cancellation="best_effort"`. |

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
