# Session Context Policy

CLIO currently treats conversational memory as session-compartmentalized state:
normal turn execution retrieves and writes context for the active session only.
That is the right default for predictable agent behavior, but it needs to be
visible to clients before we add richer memory tools.

## Backend Contract

`GET /v1/sessions/{sid}/context/policy` returns the effective policy for one
session:

```json
{
  "session_id": "sess_123",
  "memory_scope": "session",
  "writable_scope": "session",
  "cross_session_read_available": false,
  "cross_session_read_endpoint": null,
  "requires_user_consent": true,
  "notes": [
    "Conversation retrieval and writes are scoped to the active session.",
    "Cross-session memory search is not exposed by this endpoint yet.",
    "A future explicit tool may allow consented cross-session reads."
  ],
  "metadata": {
    "source": "clio_backend_default",
    "session_mode": "chat",
    "routing_mode": "auto",
    "arc_wired": false
  }
}
```

The endpoint is read-only. It does not grant memory access and it does not
change session behavior.

## Why This Exists

The memory refinement work needs two distinct concepts:

- Default session memory: safe, predictable, and scoped to the current session.
- Explicit cross-session lookup: a future tool for prompts such as "based on
  the work from the last few days," with user-visible consent and provenance.

Without a backend policy endpoint, clients have to infer this from scattered
metadata or from implementation details. That makes later TUI semantics harder
to build and easier to misrepresent.

## Future Extension

When the explicit cross-session memory search tool lands, this contract should
change from:

```json
{
  "cross_session_read_available": false,
  "cross_session_read_endpoint": null
}
```

to a truthful endpoint/tool reference, for example:

```json
{
  "cross_session_read_available": true,
  "cross_session_read_endpoint": "/v1/memory/search"
}
```

That future tool should remain opt-in, emit turn provenance, and distinguish
same-session context from imported memory snippets.
