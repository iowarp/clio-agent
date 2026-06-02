# CLIO Runtime Provenance Contract

This document defines the benchmark-facing provenance object recorded on every
GACT assistant message as `metadata.runtime_provenance`.

The contract is intentionally separate from user-facing prose. Benchmark and
research audit code should read this object, semantic events, and tool telemetry
instead of inferring CLIO behavior from final answer strings.

## Schema

Current schema version: `clio.runtime_provenance.v1`.

Required top-level fields:

- `turn`: turn id, trace id, user message id, and assistant message id.
- `workspace`: workspace id, root path, storage root, and scope label.
- `agent`: selected agent, route source/reason, and the active runtime expert.
- `blueprint`: active Agent Blueprint id, version, scope, and definition path
  when a blueprint is active.
- `provider`: provider/model ids and whether they came from expert defaults or
  global active configuration. `provider_source` and `model_source` may be
  `prompt_resolution` when a prompt profile supplied the effective runtime
  provider/model.
- `prompt`: prompt id/profile/source plus resolved prompt registry provenance.
- `tools`: declared tools, observed tool calls, full call metadata, and MCP
  descriptor/server provenance for MCP-backed declared tools.
- `commands`: declared commands and observed command invocations.
- `skills`: declared skills, resolved skill source paths, and resolution status.
- `delegation`: sync delegation lifecycle rows, including parent, child,
  return target, depth, execution mode, and duration.
- `memory`: memory policy summary and any memory search/read metadata injected
  into the turn.
- `context`: context-file provenance for files injected into the turn.
- `artifacts`: generated/proposed artifact evidence. File-diff artifacts are
  also emitted as `artifact.proposed` semantic events.
- `errors`: structured turn errors, if any.

## Delegation Semantics

For synchronous expert delegation, the parent expert asks for a child via
`expert_handoffs`. CLIO runs the child in the same session, returns a compact
child result to the parent, and re-enters the parent before finalizing the turn.

The provenance contract records this as `delegation.events` rows with:

- `stage`: `delegate.started`, `delegate.completed`, or `parent.resumed`.
- `agent_id`: active expert for the event row.
- `parent_id`: parent expert id.
- `return_to`: parent expert receiving the compact child result.
- `delegation_lifecycle`: currently `sync`.
- `depth`: nesting depth.
- `execution_mode`: `prompt_agent` or `tool_agent`.
- `duration_ms`: child execution duration when known.
- `provider`: child expert provider/model ids plus source fields.
- `prompt_resolution`: child expert prompt id/profile/source/checksum
  resolution when available.
- `agent_runtime`: compact child runtime provenance, including prompt,
  model, tools, skills, commands, pack, and Blueprint fields.

The live semantic event stream also emits `delegation.started`,
`delegation.completed`, and `delegation.parent_resumed` events. Benchmark proof
should prefer these structured events and the final `runtime_provenance`
summary over natural-language final answers.

## Relationship To Semantic Events

`metadata.runtime_provenance` is the compact per-turn summary. Semantic events
are the timeline:

- `turn.started` / `turn.completed`
- `agent.invocation.started` / `agent.invocation.completed`
- `llm.request.started` / `llm.response.completed`
- `delegation.*`
- `tool.call.*`
- `memory.*`
- `artifact.proposed`
- `command.invocation.*`

The final `turn.completed` or `turn.failed` semantic event includes the
assistant message metadata, including `runtime_provenance`.

## Benchmark Rules

Semantic-proof benchmark cases should fail when required provenance is missing.
At minimum, cases that claim hierarchy behavior should assert:

- `runtime_provenance.schema_version == "clio.runtime_provenance.v1"`.
- `turn.trace_id` matches live semantic event trace ids.
- Active blueprint id/scope/path match the session selection.
- Active expert id/source/tier/parent id match the expected hierarchy.
- Provider/model/prompt/profile fields are present for each active expert.
- Delegation rows show parent-to-child and child-to-parent sync return.
- Declared tools/skills/commands match the active expert.
- Observed tools are a subset of declared capabilities unless explicitly
  documented as builtin runtime infrastructure.
- MCP-backed tools include server/descriptor/install/trust provenance.
- Memory/context/artifact/error evidence is read from structured fields, not
  from final answer text.

## Known Limits

`commands.observed` and `artifacts` are currently conservative summary fields.
Command and artifact events already exist in the semantic stream; future
benchmark report code should fold those events into the per-turn summary if it
needs one-file evidence without replaying the semantic event timeline.
