"""A turn's completion signals publish only after its assistant message is readable.

``message.completed`` (and the transcript ``turn.completed`` beside it) is the event a
client acts on: the CLI fetches ``GET /v1/sessions/{sid}/messages/{message_id}`` the
moment it sees it. That route reads ``app.state.messages``, which the assistant message
only joins inside ``persist_finalized_message`` (``_append_session_message``). Finalize
used to publish the completion events FIRST and persist afterwards, from the finalize
executor thread, so a fast client could observe the completion and 404 on the fetch
(``tests/test_ui/test_cli_pause.py::test_ask_question_drives_permission_pause``,
``message_fetch_failed ... [404] not_found: message not found: msg_asst_...``).

These tests pin the order deterministically, with no reliance on thread timing: a spy
on the app's bus records, at the instant each completion event is published, whether
the referenced assistant message is already in the ledger the GET route reads. Both
settle paths are covered: the normal ``finalize_turn`` and the ``settle_failed_finalize``
error envelope. Assertions run at top level, never inside the turn (an exception raised
inside finalize would be absorbed by the turn's own error envelope).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

from .test_post_messages import FakeClioAgent

# #948 S4b: default sessions run the blueprint react ``main``; route it to the
# ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

_COMPLETION_TYPES = frozenset({"message.completed", "turn.completed", "turn.failed"})


def _spy_completion_publishes(app: Any) -> list[tuple[str, dict[str, Any], set[str]]]:
    """Record ``(type, payload, assistant ids readable at publish time)`` per completion."""

    observed: list[tuple[str, dict[str, Any], set[str]]] = []
    bus = app.state.bus
    real_publish = bus.publish

    def _recording_publish(event: Any, *args: Any, **kwargs: Any) -> Any:
        if event.type in _COMPLETION_TYPES:
            readable = {
                m.id for m in app.state.messages.get(event.session_id, []) if m.role == "assistant"
            }
            observed.append((event.type, dict(event.payload), readable))
        return real_publish(event, *args, **kwargs)

    bus.publish = _recording_publish
    return observed


def _run_turn_until_settled(client: TestClient, sid: str) -> str:
    """POST one turn and wait for the session to leave ``running``; return its status."""

    ack = client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})
    assert ack.status_code == 200, ack.text
    deadline = time.monotonic() + 30.0
    status = "running"
    while time.monotonic() < deadline:
        status = client.get(f"/v1/sessions/{sid}").json()["status"]
        if status != "running":
            return status
        time.sleep(0.02)
    raise AssertionError(f"turn never settled; session stayed {status!r}")


def _assert_completion_after_persist(
    observed: list[tuple[str, dict[str, Any], set[str]]],
) -> str:
    """Every completion event saw its assistant message already readable."""

    completed = [(p, r) for t, p, r in observed if t == "message.completed"]
    assert len(completed) == 1, f"expected one message.completed, got {observed!r}"
    payload, readable = completed[0]
    message_id = payload["message_id"]
    assert message_id in readable, (
        "message.completed was published before its assistant message was "
        f"readable via GET: {message_id} not in {sorted(readable)}"
    )
    for event_type, _payload, seen in observed:
        assert message_id in seen, (
            f"{event_type} was published before assistant message {message_id} was persisted"
        )
    return message_id


def test_finalize_publishes_completion_only_after_the_message_is_persisted(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    observed = _spy_completion_publishes(app)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "order"}).json()["id"]
        assert _run_turn_until_settled(client, sid) == "idle"

        message_id = _assert_completion_after_persist(observed)
        fetched = client.get(f"/v1/sessions/{sid}/messages/{message_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["stop_reason"] == "end_turn"


def test_failed_finalize_publishes_completion_only_after_the_message_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    # Runs unconditionally in the finalize region, so it routes the turn through the
    # settle_failed_finalize error envelope (same seam as test_finalize_error_envelope).
    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    observed = _spy_completion_publishes(app)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "order"}).json()["id"]
        assert _run_turn_until_settled(client, sid) == "error"

        message_id = _assert_completion_after_persist(observed)
        fetched = client.get(f"/v1/sessions/{sid}/messages/{message_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["stop_reason"] == "error"
