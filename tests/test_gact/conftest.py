"""Shared helpers for the GACT test suite.

POST /messages used to be synchronous (the response body carried
the assistant message). Since CLIO-BBBBBBBBBB-D it returns an ack
``{message_id, accepted_at}`` and the assistant turn arrives
asynchronously via SSE / GET /messages. ``complete_turn`` wraps that
flow for tests that just want "POST + return the assistant".
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient


def complete_turn(
    client: TestClient,
    sid: str,
    text: str,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.05,
) -> dict[str, Any]:
    """POST a user message, wait for the assistant turn to settle,
    return the assistant message dict.

    Polls ``GET /v1/sessions/{sid}/messages`` until either:

    - The user message we just POSTed is followed (chronologically)
      by an assistant message — that's the settled turn.
    - The deadline fires, in which case the test fails fast with
      a TimeoutError so a stuck background task doesn't hang CI.

    Tests that explicitly want to inspect the ack body should call
    POST directly. This helper is the convenience for "fire a turn
    and see what came back".
    """

    ack = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": text}]},
    )
    assert ack.status_code == 200, ack.text
    body = ack.json()
    user_id = body["message_id"]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        # Newest-first ordering: the assistant we want is the
        # message immediately *before* the user message in the list
        # (i.e. one index lower).
        for i, m in enumerate(msgs):
            if m.get("id") == user_id:
                if i > 0 and msgs[i - 1]["role"] == "assistant":
                    return msgs[i - 1]
                break
        time.sleep(poll_interval)

    raise TimeoutError(
        f"turn for user message {user_id!r} did not settle within {timeout:g}s"
    )
