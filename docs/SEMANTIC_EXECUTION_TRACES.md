# Semantic Execution Traces

CLIO emits semantic execution events so a run can be inspected as a research
workflow, not only as text deltas or token metrics. The same event spine feeds:

- live GACT SSE events with type `semantic.event`,
- configured agentic provenance providers (durable JSONL by default),
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
- `subject`: target object, for example message id, command id, or artifact id
  (a designated artifact is referenced by its registry `artifact_id` + `sha256`,
  not a hand-composed path string).
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
  `hook.invocation.blocked`, `hook.invocation.deferred`
- `hook.invoked` — the P2.7 per-invocation governance audit (one per hook run;
  trace-only, never on the live UI wire)
- `memory.search.completed`, `memory.compacted`
- `permission.requested`
- `user_question.created`
- `artifact.proposed`, `artifact.created`, `artifact.version.added`,
  `artifact.alias.moved` (the artifact `.created` family reaches the SSE UI wire),
  and the trace-only `artifact.used` / `artifact.transform.recorded` provenance
  events (`b = transform(a)` lineage — never a scraped path string)
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

Durable JSONL provenance is on by default. Live semantic SSE is also available
at the configured detail level.

## Durable JSONL Backend

Configure with:

```bash
CLIO_PROVENANCE_PROVIDERS=jsonl
CLIO_PROVENANCE_JSONL_PATH=/path/to/traces
CLIO_SEMANTIC_TRACE_DETAIL=semantic
```

`provenance.agentic.providers` (or `CLIO_PROVENANCE_PROVIDERS`) is the
authoritative provider list. The committed default is `jsonl`. Selecting
`none` or an empty list disables downstream providers but does not replace ARC,
which remains the live context substrate and semantic-event source.

The old `CLIO_SEMANTIC_TRACE_BACKEND` surface remains a compatibility input
only when the new provider list is not configured. It accepts:

- `none`: no downstream trace writes.
- `file`: append JSONL traces locally.
- `factory`: call a custom Python factory configured by
  `CLIO_SEMANTIC_TRACE_FACTORY=module:function`.

`CLIO_PROVENANCE_JSONL_PATH` and the legacy `CLIO_SEMANTIC_TRACE_PATH` can be
either a file path or a directory. Directory mode writes one file per session:

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

## Optional Flowcept Provider

Install the optional dependency and select it explicitly:

```bash
uv sync --extra flowcept
CLIO_PROVENANCE_PROVIDERS=jsonl,flowcept
FLOWCEPT_SETTINGS_PATH=/path/to/flowcept-settings.yaml
CLIO_FLOWCEPT_PRIVACY=metadata
```

Flowcept is never imported when it is not selected. CLIO gives Flowcept mapped
workflow, agent, and task records after ARC has recorded each semantic event;
Flowcept owns its configured buffering, MQ, persistence, and query services.
Provider-local filters default to excluding token/thinking event classes. The
privacy modes are `metadata` (default), `redacted`, and `full`.

Useful optional settings are:

- `CLIO_FLOWCEPT_WORKFLOW_SCOPE=session|process`
- `CLIO_FLOWCEPT_CAMPAIGN_SCOPE=session|workspace|agent`
- `CLIO_FLOWCEPT_CAMPAIGN_ID=<explicit-id>`
- `CLIO_FLOWCEPT_INCLUDE_EVENTS=<comma-separated patterns>`
- `CLIO_FLOWCEPT_EXCLUDE_EVENTS=<comma-separated patterns>`

The query surface is provider-neutral:

```text
GET /v1/provenance/providers
GET /v1/sessions/{sid}/provenance/execution?provider=native&include_children=true
GET /v1/sessions/{sid}/provenance/execution?provider=flowcept&include_children=true
```

Both execution queries return `clio.execution_provenance.v1`; clients do not
need Flowcept database or schema knowledge. Artifact lineage remains on the
separate CLIO artifact API.

## Hooks

CLIO ships ONE hook system (`clio_agent.gact.hooks`, P2.2 #1070): a declarative,
stable-id, tighten-only dispatcher over an internal adapter interface. A hook is a
declared entry, not a Python file dropped in a directory — the subprocess adapter
speaks the industry exit-0/exit-2 wire (a JSON envelope on stdin; exit 0 => parse
stdout as the tagged-union output, empty => allow; exit 2 => deny with stderr as
the model-facing reason; any other exit => a non-blocking infrastructure error).

Events this slice ships (the ports of the old registry's live events):

- `PreToolUse` — deny-capable. Fires at the tool gate AFTER the structural
  `is_read_only` fast-allow, so a hook can never gate a provably read-only call. A
  hook deny blocks the tool and its reason reaches the model.
- `UserPromptSubmit` — deny-capable. A deny vetoes the turn.
- `Stop` — post-turn observation (the old `post_message`).
- `SemanticEvent` — observation over every emitted semantic event (the old
  `semantic_event`).

CLIO emits `hook.invocation.*` semantic events around `UserPromptSubmit` and
`Stop` dispatch so trace consumers see hook activity in the timeline. A blocked
`UserPromptSubmit` hook records `hook.invocation.blocked` and `turn.failed`. In
addition, EVERY hook invocation (any event) emits exactly one `hook.invoked`
governance-audit event — see `docs/HOOKS.md`.

Configuration is a single declarative JSON file discovered at user scope
(`<user_config_dir>/hooks.json`) then project scope (`<cwd>/.clio/hooks.json`),
merged so a project entry overrides a user entry with the same `id`. Set
`CLIO_HOOKS_CONFIG` to point at one explicit file instead. Each entry:

```json
{
  "version": 1,
  "hooks": [
    {
      "id": "block-secrets",
      "on": ["PreToolUse"],
      "match": { "tool": "^(fs_apply_edit_write)$",
                 "annotations": { "destructive": true },
                 "argsPattern": "\\.env" },
      "run": { "type": "command", "command": "./hooks/secrets.sh" },
      "timeoutMs": 30000,
      "failClosed": true,
      "enabled": true
    }
  ]
}
```

Invariants: a required stable `id` (never positional); tool regexes are anchored
by default (`Edit` does not match `NotebookEdit`); annotation matching against the
wire `tool_annotations` block covers MCP tools nobody enumerated (fail-safe
defaults per MCP); a hook may only TIGHTEN (a hook `allow` never lifts a policy
deny); a hook FAILURE is distinct from a user rejection (a timeout/crash for a
deny-capable `failClosed` hook denies with a typed "not a user rejection"
message); most-restrictive-wins across N hooks (`deny > ask > allow`).

`/v1/capabilities` reports `x_clio_hook_backend` (`declarative`) and
`x_clio_hook_events` (per-event handler counts) so clients can see the active
dispatcher and its configured hooks. Every degraded path (timeout, crash, missing
binary, unparseable stdout, fail-closed deny) records a typed reason queryable via
`clio_agent.gact.hooks.hook_reasons()` and the `hook.fallback` audit line.

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
