# Context File Persistence

Session context-file attachments are persisted next to the GACT session store in
`context_files.json`. This keeps the backend context ledger stable across CLIO
GACT restarts.

## Stored Shape

The file stores rows by session id and path:

```json
{
  "sessions": {
    "sess_123": {
      "/workspace/notes.md": {
        "path": "/workspace/notes.md",
        "mode": "read",
        "added_at": "2026-05-27T00:00:00+00:00",
        "last_modified": "",
        "size": 128,
        "language": "markdown"
      }
    }
  }
}
```

## Write Points

- `POST /v1/sessions/{sid}/context/files` upserts and flushes the ledger.
- `DELETE /v1/sessions/{sid}/context/files` removes an attached path and
  flushes the ledger.
- `DELETE /v1/sessions/{sid}` removes that session's context-file bucket and
  flushes the ledger.

Malformed persisted rows are ignored on load so a corrupt attachment row does
not prevent the session registry from starting.
