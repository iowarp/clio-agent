# Semantic Execution Traces

CLIO emits semantic execution events so a run can be inspected as a research
workflow, not only as text deltas or token metrics. The same event spine feeds:

- live GACT SSE events with type `semantic.event`,
- optional durable JSONL traces,
- Python runtime hooks named `semantic_event(event)`.

## Event Schema

Each event payload uses schema version `clio.semantic_event.v1`.

Required top-level fields:

- `schema_version`: stable schema identifier.
- `event_id`: unique event/span id.
- `event_type`: namespaced event name such as `turn.started`,
  `llm.request.started`, `delegation.completed`, or `tool.call.completed`.
- `session_id`, `workspace_id`, `trace_id`, `turn_id`: provenance and grouping.
- `span_id`, `parent_span_id`: timeline/tree rendering ids.
- `status`: `running`, `completed`, `failed`, `blocked`, `pending`, or similar.
- `summary`: short user-safe description.
- `actor`: caller/producer, for example expert id, hook name, or tool name.
- `subject`: target object, for example message id, command id, artifact path.
- `blueprint`: active Agent Blueprint/pack provenance when known.
- `provider`: provider/model/config provenance when relevant.
- `payload`: event-specific structured details.
- `live_observed`: true when emitted at the runtime boundary instead of rebuilt
  after the final answer.
- `detail_level`: effective redaction/detail policy.
- `occurred_at`: UTC timestamp.

## Event Types

Current CLIO events include:

- `turn.started`, `turn.completed`, `turn.failed`
- `agent.invocation.started`, `agent.invocation.completed`
- `llm.request.started`, `llm.response.completed`
- `delegation.started`, `delegation.completed`, `delegation.parent_resumed`,
  `delegation.failed`
- `tool.call.started`, `tool.call.completed`
- `hook.invocation.started`, `hook.invocation.completed`,
  `hook.invocation.failed`, `hook.pre_message.blocked`
- `memory.search.completed`, `memory.compacted`
- `permission.requested`
- `user_question.created`
- `artifact.proposed`
- `command.invocation.completed`, `command.invocation.failed`,
  `command.invocation.denied`
- `subagent.started`, `subagent.completed`

## Detail Levels

Configure with `CLIO_SEMANTIC_TRACE_DETAIL`.

- `off`: semantic event sink suppresses events.
- `metadata`: emits ids, status, summary, and timestamps, but no actor/subject
  detail payloads.
- `semantic`: default. Emits structured provenance while redacting sensitive or
  high-volume values such as prompts, user text, tool args, raw responses, file
  contents, tokens, and secrets.
- `full_debug`: emits full JSON-safe payloads. This can include prompts,
  responses, user text, tool arguments, command input, and file contents, so it
  should be enabled only for trusted debugging or benchmark capture.

Durable tracing is off by default. Live semantic SSE is available by default at
the configured detail level.

## Durable JSONL Backend

Configure with:

```bash
CLIO_SEMANTIC_TRACE_BACKEND=file
CLIO_SEMANTIC_TRACE_PATH=/path/to/traces
CLIO_SEMANTIC_TRACE_DETAIL=semantic
```

`CLIO_SEMANTIC_TRACE_BACKEND` accepts:

- `none`: default, no durable trace writes.
- `file`: append JSONL traces locally.
- `factory`: call a custom Python factory configured by
  `CLIO_SEMANTIC_TRACE_FACTORY=module:function`.

`CLIO_SEMANTIC_TRACE_PATH` can be either a file path or a directory. Directory
mode writes one file per session:

```text
<session_id>.semantic.jsonl
```

For custom factories, `CLIO_SEMANTIC_TRACE_CONFIG` may contain a JSON object.
CLIO calls:

```python
factory(default_root=Path(...), config={...})
```

The returned object must expose `emit(event)` and may expose `name`. This keeps
OTEL, database, or user-provided sinks behind the same event sink as the local
file backend.

## Hooks

The runtime hook registry supports a side-effect hook:

```python
def semantic_event(event: dict) -> None:
    ...
```

Hook failures for `semantic_event` are fail-open and swallowed, matching audit
hook behavior. Policy/enforcement hooks should continue to use explicit
pre-event hooks such as `pre_tool` or `pre_message`.

CLIO also emits `hook.invocation.*` semantic events around `pre_message` and
`post_message` dispatch so trace consumers can see hook activity in the run
timeline. Matched handlers are included in the event payload with hook source,
scope, checksum, installed path, and invocation status. A blocked `pre_message`
hook records the same handler provenance on `hook.pre_message.blocked` and
`turn.failed`.

Runtime hook loading is configured through a small backend factory:

```bash
CLIO_HOOKS_BACKEND=local_python
CLIO_HOOKS_DIR=/path/to/hooks
CLIO_HOOK_TIMEOUT_S=5.0
```

`CLIO_HOOKS_BACKEND` accepts:

- `local_python`: default. Load Python files from `CLIO_HOOKS_DIR` or the XDG
  default hook directory.
- `none`: disable runtime hook dispatch.
- `factory`: load a custom Python factory from `CLIO_HOOKS_FACTORY` using
  `module:function` syntax. The factory returns a HookRegistry-compatible
  object with `fire()` and `count()`.

Local Python hooks can be global or scoped:

```text
hooks/
  pre_message.py
  post_message.py
  workspaces/<workspace_id>/pre_message.py
  sessions/<session_id>/post_message.py
  blueprints/<blueprint_id>/semantic_event.py
```

Global hooks always run. Scoped hooks run only when the runtime dispatch has
matching scope metadata. Message hooks currently provide session, workspace, and
active Blueprint ids. `semantic_event` hooks infer session/workspace scope from
the event payload.

Agent Blueprints may package Python hooks as `hooks/<event>.py`. They are
reported as disabled descriptors and require an explicit enable/trust call before
CLIO copies them into `blueprints/<blueprint_id>/` and reloads the local Python
runtime hook registry. The enablement path also writes sidecar metadata so
semantic traces can attribute a runtime hook invocation back to the source
Blueprint file. See [Agent Blueprint Packaged Hooks](AGENT_BLUEPRINT_PACKAGED_HOOKS.md).

`/v1/capabilities` reports `x_clio_hook_backend` and
`x_clio_hook_events` so clients can see the configured backend and handler
counts.

`CLIO_HOOK_TIMEOUT_S` bounds individual hook calls. Pre-event hook timeouts fail
closed through the same `PermissionError` path as other pre-hook failures.
Post-event and `semantic_event` hook failures are fail-open side effects.

## Benchmark Reports

`scripts/run_demo_benchmark.py` consumes replayed `semantic.event` SSE history
after each case and stores the compact proof in each JSONL row:

- `semantic_trace`: event counts, live-observed counts, event types, trace ids,
  and turn ids.
- `semantic_events`: the captured semantic event payloads for the completed
  turn.

The generated Markdown report summarizes semantic trace coverage in the
Evidence Summary and lists per-case semantic event types in the demo details.
This lets benchmark review cite trace evidence directly instead of relying on
final-answer string heuristics.

## TUI Consumption

The TUI can subscribe to `/v1/sessions/{sid}/events` and render every event with
`type == "semantic.event"` as a progress timeline. These events are independent
of provider token streaming, so they continue to arrive when text is delivered
only at final completion.
