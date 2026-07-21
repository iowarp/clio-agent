# S4 spawn-substrate wire traces (NDP / EarthScope)

Full, real captured GACT wire traces from the #948 S4 live gate (2026-07-18),
for UI prototyping against the **agents-creating-agents** semantics: react
mains that answer directly and spawn declared children as REAL child sessions
with durable task records. Captured on the accepted substrate (real clio-core
CTE daemon, `claude_code`/sdk provider, sonnet) from the migrated marketplace
packs (pin `5ec9bf5`). Event schema reference:
[`docs/SEMANTIC_EXECUTION_TRACES.md`](../../SEMANTIC_EXECUTION_TRACES.md).

## What each capture contains

| file | contents |
|---|---|
| `sse-received.jsonl` | The full SSE stream, one event per line, exactly as a client receives it (`event_type`, `payload`, receive timestamps). This is the primary artifact. |
| `messages.json` | The parent session transcript at end of turn — the assistant `Message` with its ordered `parts` (`thinking`, `text`, `expert_handoff`, `routing_decision`, `tool_call`, …). What a transcript renderer consumes. |
| `sessions.json` | Session records incl. the child sessions (`session_type: "agent_task"`, `parent_session_id` lineage) and the embedded `agent_task` metadata block (the durable task record: `task_id`, `agent_ref`, `depth`, `status`, `result{message_ref, answer_excerpt, workflow_state}`). |
| `sse-summary.json` | Probe roll-up: event-type counts, latency maxima, leak count. |

## The choreography to prototype (earthscope-canonical)

The canonical LA one-shot: react `main` (4 declared children) sequences its
work, spawning each child as a real child turn and composing the final answer
itself. Per child, the wire shows this exact vocabulary:

```
agent.task.queued            → task record minted (child session exists)
agent.task.started           → child turn running (dedicated per-depth pool)
blueprint.delegation.started → "main spawned geospatial" (actor=parent, subject=child)
  ... child runs in ITS OWN session; its events stream on the child channel ...
agent.task.completed         → durable result on the task record
blueprint.delegation.completed      → child's output returned to parent (VERBATIM)
blueprint.delegation.parent_resumed → re-pin the active-agent indicator to the parent
```

Interleaved with `react.step.completed` (the main's loop steps:
`spawn_agent_task`, `wait_agent_tasks`, `check_agent_tasks`,
`spawn_agents_parallel`) and `expert.lifecycle.started` /
`expert.extract.completed` bracketing the main's forward. The transcript
(`messages.json`) carries one `expert_handoff` Part per delegation edge: a
`delegate.started` header Part (parent → child, with the task text) and one
terminal return Part per child (`stage: delegate.completed`, outcome on
`status`, verbatim `output` + typed `workflow_state` in metadata) — the
delegation header / nesting / return-row render drives off these Parts.

The final `text` Part authored by `agent_id: "main"` IS the user deliverable
(no synthesis child exists anymore). `turn.completed` + `stop_reason:
"end_turn"` close the turn.

## The four captures

- **`earthscope-canonical/`** — the passing NDP run: 4 children
  (geospatial → data → analysis → visualization), all completed, evidence-backed
  final answer (155 in-region stations, station MTA1, staged CSV + source URL).
  Zero degrades, zero contract-marker leaks. Also includes
  `semantic-sse-audit.json` (the leak/structure audit output).
- **`wildfire-depth2/`** — nested orchestration: `main` → `data` (depth 1) →
  `air_quality` (**depth 2**). Task records carry `depth`; each depth runs on
  its own executor pool. NOTE: this capture's `sse-received.jsonl` ends at the
  probe's 20-min timeout while the deep chain was still working (the turn
  completed after; `messages.json`/`sessions.json` were re-fetched at
  settlement and show the final state) — a realistic long-turn case for UI
  reconnect/resume design (`Last-Event-ID` replay).
- **`data-semantics/`** — the simplest shape: a react main answering directly
  with honest did-vs-would framing; minimal delegation.
- **`earthscope-failure-modes/`** — a deliberately-kept FAILING run (an
  earlier gate iteration): child `data` fails typed (`agent.task.failed` +
  `blueprint.delegation.failed` + `parent_resumed`), the main retries it
  (model-decided), then the turn itself fails typed
  (`turn.failed` with `error_info.error: "provider_error"`, an empty-answer
  guard). Use this for error-state wireframes: failed-child return rows, task
  records with `error_reason`, a turn that ends in `stop_reason: "error"` with
  a structured `error_info`.

## Things the wire guarantees (server-side contracts the UI can rely on)

- Delegation `output` is the child's answer **byte-for-byte** (#880); a
  degraded fallback is explicitly marked (`output_source: "excerpt_fallback"`,
  `output_fallback_reason`) — never silent.
- Terminal delegation events and return Parts are **once-per-task** (re-waits
  do not duplicate them).
- Every degradation carries a typed reason; typed turn failures carry
  `error_info{error, message, details.recovery_actions}` (e.g.
  `blueprint_root_disabled`, `no_resolvable_agent`, `provider_error`).
- Child transcripts are ordinary sessions: `GET /v1/sessions/{child_sid}/messages`
  (the child sid rides the task record and the handoff Part metadata). Task
  records: `GET /v1/sessions/{sid}/agent-tasks`, `GET /v1/agent-tasks/{id}`.

Captured with `apps/web/scripts/probe-earthscope-sse.mjs` (gact-tui) against a
`spawn/s4-main-as-react` build (merged as clio-agent#984); see that PR for the
full gate report.
