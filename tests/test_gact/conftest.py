"""Shared helpers for the GACT test suite.

POST /messages used to be synchronous (the response body carried
the assistant message). It now returns an ack
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


def _fold_published_parts(history: list[Any], message_id: str) -> list[dict[str, Any]] | None:
    """Fold ``message.*`` wire events into the parts list a client would hold.

    Returns ``None`` when ``message_id`` was never minted on this bus as a
    turn's assistant message (a ``message.created`` with ``role: assistant``
    and EMPTY parts — direct API inserts and the #756 envelope's fresh error
    message carry no such event, and a trimmed history loses it first).
    """

    minted = False
    parts: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in history:
        payload = event.payload
        if event.type == "message.created":
            if (
                payload.get("id") == message_id
                and payload.get("role") == "assistant"
                and not payload.get("parts")
            ):
                minted = True
            continue
        if payload.get("message_id") != message_id:
            continue
        if event.type == "message.part.added":
            part = dict(payload.get("part") or {})
            folded = {
                "id": part.get("id", ""),
                "type": part.get("type", ""),
                "agent_id": part.get("agent_id", ""),
                "text": part.get("text", "") or "",
                "stream_source": str((part.get("metadata") or {}).get("stream_source") or ""),
                "live_text": (
                    payload.get("stream_source") == "live"
                    and part.get("type") in {"text", "thinking"}
                ),
                "completed": False,
            }
            parts.append(folded)
            by_id[folded["id"]] = folded
        elif event.type == "message.part.delta":
            folded = by_id.get(str(payload.get("part_id") or ""))
            if folded is not None:
                folded["text"] += str((payload.get("delta") or {}).get("text_append") or "")
        elif event.type == "message.part.completed":
            folded = by_id.get(str(payload.get("part_id") or ""))
            if folded is not None:
                folded["completed"] = True
                folded["text"] = str(payload.get("final_text") or "")
    if not minted:
        return None
    # A live streamed text/thinking part that never completed was dropped from
    # the ledger (empty after clean) — a fold consumer discards it too, so
    # live and reload cannot disagree (design §4 row 4).
    return [p for p in parts if not (p["live_text"] and not p["completed"])]


@pytest.fixture(autouse=True)
def _live_equals_reload_property(monkeypatch):
    """#767 PR3 (design §8.2b): live == reload, enforced for EVERY gact turn.

    Folding the published ``message.created`` / ``part.added`` / ``part.delta``
    / ``part.completed`` stream must reconstruct the persisted assistant
    ``Message.parts`` field-for-field (id, arrival-order sequence, type,
    agent_id, text/final_text, stream_source). Wraps the persistence funnel so
    every existing scenario (streaming, SSE, thinking blocks, delegation,
    cancellation) doubles as a regression for the reconciliation-drift class
    this epic ends.
    """

    from clio_agent.gact import app as gact_app

    violations: list[str] = []
    real_append = gact_app._append_session_message

    def _checked_append(app, sid, msg, *args, **kwargs):
        result = real_append(app, sid, msg, *args, **kwargs)
        try:
            if getattr(msg, "role", "") != "assistant":
                return result
            history = list(app.state.bus._history.get(sid, []))
            folded = _fold_published_parts(history, msg.id)
            if folded is None:
                return result
            persisted = [
                {
                    "id": part.id,
                    "sequence": part.sequence,
                    "type": part.type,
                    "agent_id": part.agent_id,
                    "text": part.text or "",
                    "stream_source": str(part.metadata.get("stream_source") or ""),
                }
                for part in msg.parts
            ]
            reconstructed = [
                {
                    "id": p["id"],
                    "sequence": index,
                    "type": p["type"],
                    "agent_id": p["agent_id"],
                    "text": p["text"],
                    "stream_source": p["stream_source"],
                }
                for index, p in enumerate(folded, start=1)
            ]
            if reconstructed != persisted:
                violations.append(
                    f"live==reload violated for message {msg.id!r} (session {sid!r}):\n"
                    f"  folded SSE stream -> {reconstructed}\n"
                    f"  persisted parts   -> {persisted}"
                )
        except Exception as exc:  # noqa: BLE001 - the property check must never mask the turn
            violations.append(f"live==reload check crashed for {getattr(msg, 'id', '?')!r}: {exc!r}")
        return result

    monkeypatch.setattr(gact_app, "_append_session_message", _checked_append)
    yield
    assert not violations, "\n".join(violations)


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
