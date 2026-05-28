# Session Context Attachment Lifecycle

Session context files are part of CLIO's working context, not transient UI
state. They must survive the same session lifecycle operations as messages.

## Preserved Operations

- `POST /v1/sessions/{sid}/fork` copies the source session's context-file
  ledger into the fork.
- `GET /v1/sessions/{sid}/export` includes a top-level `context_files` array.
- `POST /v1/sessions/import` restores exported `context_files` onto the new
  imported session id.

## Shape

Each row is the same shape returned by
`GET /v1/sessions/{sid}/context/files`, for example:

```json
{
  "path": "/workspace/notes.md",
  "mode": "read",
  "added_at": "2026-05-27T00:00:00+00:00",
  "last_modified": "",
  "size": 128,
  "language": "markdown"
}
```

Import is intentionally tolerant: malformed rows and rows without `path` are
ignored, matching the existing message import behavior.
