# Cross-Session Memory Search

Tracking issue: https://github.com/iowarp/clio-agent/issues/331

## Purpose

CLIO normally keeps runtime context session-scoped. That is the right default:
a turn should not silently pull unrelated transcript content into the model.

Some workflows still need an explicit way to answer questions such as "based on
the work from the last few days, continue with X". For that case CLIO exposes a
search surface that can cross session boundaries only when the caller opts in.

## Endpoint

`GET /v1/memory/search`

Query parameters:

- `query`: required search text.
- `session_id`: current session. Required unless `include_cross_session=true`.
- `workspace_id`: optional workspace filter.
- `include_cross_session`: defaults to `false`.
- `limit`: result cap, clamped to `1..100`, default `20`.

Default behavior is session-scoped:

```text
GET /v1/memory/search?query=pressure&session_id=sess_a
```

Cross-session behavior is explicit:

```text
GET /v1/memory/search?query=pressure&session_id=sess_a&include_cross_session=true
```

## Response Semantics

Each hit includes:

- `session_id`, `session_title`, and `workspace_id`;
- `message_id`, `part_id`, `role`, and timestamps;
- a bounded text excerpt;
- `score` and `match_terms`;
- metadata including `source=gact_transcript` and whether the hit crossed away
  from the requested `session_id`.

The endpoint searches retained GACT transcript memory. It does not yet search
ARC procedural memories, dataset profiles, compact-memory events, or future
context-frame ledgers. Those can be added behind the same response shape by
using a different `metadata.source` value.

## Safety Rules

- Cross-session search is never implicit.
- Unknown `session_id` returns `404`.
- Empty or punctuation-only queries return `422`.
- Callers should show provenance before using a hit as model context.
- Orchestrator/TUI callers may inject hits into a turn only through explicit
  message metadata:

```json
{
  "memory_search": {
    "enabled": true,
    "query": "pressure dataset",
    "include_cross_session": true,
    "workspace_id": "ws_science",
    "limit": 5,
    "reason": "answer user request about recent work"
  }
}
```

When this metadata is present, CLIO prepends an `Explicit Memory Search Results`
section to the agent input, emits a `memory.search.completed` event, and records
the same provenance under assistant `metadata.memory_search`. This makes
cross-session recall available to the model while keeping it visible and
auditable.
