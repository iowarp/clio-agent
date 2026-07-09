# clio_agent.sdk

Typed Python client for the GACT v1 API — talk to a running clio
backend (default `http://127.0.0.1:8100`) from code. Pure client: it
never imports the gact server. Wire shapes follow the reconciled
contract (`gact-tui/contract/SPEC.md`).

## Quickstart

```python
from clio_agent.sdk import ClioClient, MessageCompleted, MessagePartDelta

with ClioClient("http://127.0.0.1:8100") as client:
    # Capability truth: probe before using optional surfaces.
    caps = client.capabilities()
    assert caps.supports("sessions")

    session = client.sessions.create(title="demo")
    client.messages.post(session.id, text="What files are in my workspace?")

    # Stream the turn live; resume-safe via Last-Event-ID.
    with client.sessions.events(session.id, reconnect_attempts=3) as stream:
        for event in stream:
            if isinstance(event, MessagePartDelta):
                print(event.text_append, end="", flush=True)
            elif isinstance(event, MessageCompleted):
                break

    for perm in client.permissions.list(status="pending").permissions:
        client.permissions.respond(perm.id, "allow")
```

Errors mirror the §14 taxonomy: `NotFoundError`, `InvalidRequestError`,
`ConflictError`, `PermissionDeniedError`, `UnsupportedError`,
`ServiceUnavailableError`, `InternalServerError` — all subclasses of
`ClioAPIError` carrying `error` (tag), `details`, `recoverable`, and
`retry_after_s`. Nothing retries silently: `retries=` (GET transport
failures) and `reconnect_attempts=` (SSE drops) are opt-in, bounded,
and logged on `logging.getLogger("clio_agent.sdk")`.
