# Memory Context Attachment Stats

`GET /v1/memory/stats?session_id=...` reports the active session's retained
message/token pressure. It now also reports the files attached to the session's
context ledger:

```json
{
  "session": {
    "session_id": "sess_123",
    "messages_retained": 4,
    "tokens_retained": 812,
    "tokens_budget": 4000,
    "profiles_attached": 0,
    "context_files_attached": 3,
    "context_files_by_mode": {
      "edit": 1,
      "pin": 1,
      "read": 1
    }
  }
}
```

The counts are intentionally lightweight. Clients can use them for footer
status, memory/context sidebars, and warning thresholds without fetching full
file metadata on every refresh. The authoritative attachment list remains
`GET /v1/sessions/{sid}/context/files`.

Unknown sessions keep the existing polling contract: the endpoint returns `200`
with an empty session block rather than a `404`.
