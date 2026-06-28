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

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _default_test_arc(request, tmp_path, monkeypatch):
    """Give every GACT test app a real, per-app ARC unless it asked otherwise.

    ARC is the source of the highway: ``_emit_semantic_event`` routes EVERY event through
    ``arc.record_semantic_event``, which records ARC's view and then DERIVES the highway
    (trace/SSE/hooks) via the sink wired by ``_set_app_arc``. It now fails loud when no ARC
    is reachable, so the many tests that build ``build_app(...)`` without an ``arc`` -- and
    which previously relied on a silent ``sink.emit`` bypass -- need a real ARC. A real
    ``ARCMemory`` (not a stub) is used because the GACT layer also drives its op-logger,
    cache-stats and live-fold surfaces, which the stub would not provide.

    Wraps the ``build_app`` bound in the test module so a call that OMITS ``arc`` gets a
    fresh ``ARCMemory`` rooted in the test's ``tmp_path`` (each app its OWN arc, so the
    derive sink wires to that app's bus). Calls that pass ``arc=`` explicitly -- including
    the ones that exercise the arc-absent path with ``arc=None`` -- are left as written.
    """
    module = request.module
    real_build_app = getattr(module, "build_app", None)
    if real_build_app is None:
        yield
        return

    from clio_agent.arc.live import _MemoryStore
    from clio_agent.arc.memory import ARCMemory

    counter = {"n": 0}

    def _build_app_with_default_arc(*args, **kwargs):
        if "arc" not in kwargs:
            counter["n"] += 1
            # In-memory ARCStore: a real ARCMemory (so the op-logger / cache-stats /
            # live-fold surfaces the GACT layer drives are all present), but with no
            # filesystem persistence. The latter matters because the live tool observer
            # runs ON the turn thread and the per-turn settle reads the same in-process
            # ledger; an FS-backed record on the emit path adds I/O latency that races
            # that ordering. The in-memory store keeps record fast, as the old in-memory
            # sink fallback was, so the per-test ordering assertions hold.
            kwargs["arc"] = ARCMemory(
                data_dir=str(tmp_path / f"arc_{counter['n']}"),
                store=_MemoryStore(),
            )
        return real_build_app(*args, **kwargs)

    monkeypatch.setattr(module, "build_app", _build_app_with_default_arc)
    yield


def complete_turn(
    client: TestClient,
    sid: str,
    text: str,
    *,
    json_override: dict[str, Any] | None = None,
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

    body = {"parts": [{"type": "text", "text": text}]}
    if json_override:
        body.update(json_override)
    ack = client.post(f"/v1/sessions/{sid}/messages", json=body)
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
                if (
                    i > 0
                    and msgs[i - 1]["role"] == "assistant"
                    and not msgs[i - 1].get("metadata", {}).get("live")
                ):
                    return msgs[i - 1]
                break
        time.sleep(poll_interval)

    raise TimeoutError(f"turn for user message {user_id!r} did not settle within {timeout:g}s")
