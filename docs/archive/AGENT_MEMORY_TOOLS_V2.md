# Agent Memory Tools V2

Tracks GitHub issue #369.

## Goal

Expose memory as explicit orchestrator-callable tools. The model should be able
to answer requests such as "based on the work from the last few days" by
searching prior sessions in the permitted scope, reading summaries/context
frames, and injecting provenance-bearing memory into the current turn.

The existing `/v1/memory/search` endpoint is foundation only. V2 makes memory a
controlled agent capability.

## Default Scope

Normal execution is session-local. Cross-session memory is opt-in by user intent
or policy and defaults to the active workspace.

Rules:

- same session: always available to normal context assembly
- same workspace: available when user intent or policy permits
- global sessions: available for global/user-insight requests or global scope
- other workspace: denied unless a future explicit policy grants it

Workspace semantics are a guardrail, not the main feature. The main feature is
agent-callable memory with provenance.

## Tools

### `memory_search_sessions`

Input:

```json
{
  "query": "work from the last few days on NDP",
  "scope": "current_workspace",
  "limit": 10,
  "filters": {
    "since": "",
    "agent_id": "",
    "expert_id": ""
  }
}
```

Output includes bounded hits with session id, title, workspace/global scope,
score, matched terms, summary excerpt, and provenance.

### `memory_read_session_summary`

Reads a compact session summary and metadata. It must not dump full raw
transcripts.

### `memory_read_context_frame`

Reads a selected context frame or durable memory artifact with source metadata.

Raw message reads are out of scope for V2 unless added behind stricter policy.

## Context Injection

When memory results are used in a turn, context-frame metadata records:

- memory tool called
- query/filters
- session/frame ids read
- scope and policy decision
- snippets injected
- tombstone/exclusion handling

## Undo/Rewind

Search and reads must respect deleted/rewound transcript ranges. If a future
policy preserves tombstoned metadata for audit, the agent-facing memory tools
must not reintroduce removed content as normal context.

## Acceptance Criteria

- Agent can search and read prior same-workspace memory when user intent permits.
- Other-workspace memory calls are denied with structured policy errors.
- Global sessions are read only under global/user-level intent or scope.
- Search results are bounded and provenance-bearing.
- Context-frame reads include source and policy provenance.
- Undo/rewind exclusions are respected.
- Tests cover same-session, same-workspace, global, denied workspace, and
  tombstone behavior.

