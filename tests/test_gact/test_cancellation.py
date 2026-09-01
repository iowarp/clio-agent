"""POST /v1/sessions/{sid}/cancel.

Two scenarios:
- Idle session: status -> cancelled, event fired, 204; a later user turn starts fresh.
- Unknown session: 404 with the v0.2 error envelope.
- In-flight turn: cancellation settles that turn without poisoning the next one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str):
        return _Pred()


class _CountingAgent:
    def __init__(self) -> None:
        self.calls = 0

    def forward(self, question: str, session_id: str):
        self.calls += 1
        return _Pred()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))


def test_cancel_flips_status_and_publishes_event(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sess = client.post("/v1/sessions", json={"title": "t"}).json()
    sid = sess["id"]

    resp = client.post(f"/v1/sessions/{sid}/cancel")
    assert resp.status_code == 204
    assert resp.content == b""

    # Session now reports cancelled.
    row = client.get(f"/v1/sessions/{sid}").json()
    assert row["status"] == "cancelled"
    status_events = [
        e
        for e in client.app.state.bus._history.get(sid, [])
        if e.type == "session.status_changed" and e.payload.get("status") == "cancelled"
    ]
    attempt = status_events[-1].payload["cancellation_attempt"]
    assert attempt["session_id"] == sid
    assert attempt["in_flight"] is False
    assert attempt["hard_abort_supported"] is False
    assert attempt["upstream_abort"] == "not_supported"
    assert attempt["executor_work_may_continue"] is False


def test_cancel_unknown_session_404s_with_v0_2_envelope(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.post("/v1/sessions/sess_nope/cancel")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "session not found" in body["error"]["message"]


class _SlowAgent:
    """Agent that sleeps long enough for /cancel to race in."""

    def __init__(self, sleep_s: float = 5.0) -> None:
        self.sleep_s = sleep_s
        self.completed = False

    def forward(self, question: str, session_id: str):
        import time

        time.sleep(self.sleep_s)
        self.completed = True
        return type(
            "Pred", (), {"answer": "late", "selected_expert": "", "routing_rationale": ""}
        )()


class _LateToolObserverAgent:
    """Agent that reports a successful tool completion after cancellation."""

    def __init__(self, sleep_s: float = 0.4) -> None:
        import threading

        self.sleep_s = sleep_s
        self.started = threading.Event()
        self.completed = threading.Event()

    def forward(self, question: str, session_id: str):
        import time

        from clio_agent.tools.execution import notify_global_tool_observer

        notify_global_tool_observer("late_tool", {"question": question}, "started", None)
        self.started.set()
        time.sleep(self.sleep_s)
        notify_global_tool_observer("late_tool", {"question": question}, "completed", None)
        self.completed.set()
        return type(
            "Pred", (), {"answer": "late", "selected_expert": "", "routing_rationale": ""}
        )()


def test_cancel_during_turn_marks_turn_as_cancelled(tmp_path: Path) -> None:
    """Cancelling the asyncio task does not kill executor-thread work."""

    import time as _time

    agent = _SlowAgent(sleep_s=0.6)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        # Fire turn (returns ack immediately; turn runs in background).
        c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        # Give the loop a slice to schedule the task + start the
        # blocking sleep in the executor.
        _time.sleep(0.1)
        c.post(f"/v1/sessions/{sid}/cancel")
        # Poll for the assistant turn to settle as cancelled.
        # complete_turn polls list_messages — the assistant
        # appears once the cancellation path finalises.
        # We just want the GACT envelope to settle promptly.
        deadline = _time.monotonic() + 3.0
        assistant = None
        while _time.monotonic() < deadline:
            msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            assistants = [
                m
                for m in msgs
                if m["role"] == "assistant" and not m.get("metadata", {}).get("live")
            ]
            if assistants:
                assistant = assistants[0]
                break
            _time.sleep(0.1)
        assert assistant is not None, "cancel didn't settle the turn within 3s"
        assert assistant["error_info"]["error"] == "cancelled"
        assert assistant["error_info"]["details"]["execution_cancellation"] == "best_effort"
        assert assistant["error_info"]["details"]["executor_work_may_continue"] is True
        assert assistant["error_info"]["details"]["hard_abort_supported"] is False
        assert assistant["error_info"]["details"]["upstream_abort"] == "not_supported"
        attempt = assistant["error_info"]["details"]["cancellation_attempt"]
        assert attempt["session_id"] == sid
        assert attempt["in_flight"] is True
        assert attempt["cooperative_signal_sent"] is True
        assert attempt["asyncio_task_cancel_scheduled"] is True
        assert attempt["executor_work_may_continue"] is True
        status_events = [
            e
            for e in app.state.bus._history.get(sid, [])
            if e.type == "session.status_changed" and e.payload.get("status") == "cancelled"
        ]
        assert status_events
        assert status_events[-1].payload["execution_cancellation"] == "best_effort"
        assert status_events[-1].payload["executor_work_may_continue"] is True
        assert status_events[-1].payload["cancellation_attempt"]["id"] == attempt["id"]

        # The executor thread can still finish after the GACT envelope
        # has truthfully settled as cancelled.
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline and not agent.completed:
            _time.sleep(0.05)
        assert agent.completed is True


def test_cancel_before_turn_skips_agent_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation committed at the turn boundary prevents provider work."""

    import time as _time

    from clio_agent.gact import turn as turn_module

    original_make_turn_cancel_event = turn_module.make_turn_cancel_event

    def cancel_at_turn_boundary(state: Any) -> None:
        original_make_turn_cancel_event(state)
        state.app.state.cancel_flags.add(state.sid)
        state.turn_cancel_event.set()

    monkeypatch.setattr(turn_module, "make_turn_cancel_event", cancel_at_turn_boundary)
    agent = _CountingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "cancel before forward"}).json()["id"]
        response = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "do not forward"}]},
        )
        assert response.status_code == 200

        deadline = _time.monotonic() + 3.0
        assistant = None
        while _time.monotonic() < deadline:
            messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            assistant = next(
                (
                    message
                    for message in messages
                    if message["role"] == "assistant"
                    and message.get("error_info", {}).get("error") == "cancelled"
                ),
                None,
            )
            if assistant is not None:
                break
            _time.sleep(0.05)

        assert assistant is not None, "turn-boundary cancellation did not settle"
        assert agent.calls == 0
        assert assistant["error_info"]["details"]["execution_cancellation"] == "turn_boundary"
        assert assistant["error_info"]["details"]["executor_work_may_continue"] is False


def test_late_tool_completion_after_cancel_is_not_reported_as_success(
    tmp_path: Path,
) -> None:
    """Late observer completions must not become success telemetry or stale metadata."""

    import time as _time

    from .conftest import complete_turn

    agent = _LateToolObserverAgent(sleep_s=0.35)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert agent.started.wait(timeout=2.0)
        c.post(f"/v1/sessions/{sid}/cancel")

        deadline = _time.monotonic() + 3.0
        assistant = None
        while _time.monotonic() < deadline:
            msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            assistants = [
                m
                for m in msgs
                if m["role"] == "assistant" and m.get("error_info", {}).get("error") == "cancelled"
            ]
            if assistants:
                assistant = assistants[0]
                break
            _time.sleep(0.05)
        assert assistant is not None, "cancel didn't settle the turn within 3s"
        assert assistant["error_info"]["error"] == "cancelled"
        assert agent.completed.wait(timeout=2.0)

        completed_events = [
            e
            for e in app.state.bus._history.get(sid, [])
            if e.type == "tool.call.completed" and e.payload.get("tool") == "late_tool"
        ]
        assert completed_events
        assert not any(e.payload.get("ok") is True for e in completed_events)
        assert completed_events[-1].payload["ok"] is False
        assert completed_events[-1].payload["execution_cancellation"] == "best_effort"
        assert completed_events[-1].payload["executor_work_may_continue"] is True

        app.state.agent = _Agent()
        next_assistant = complete_turn(c, sid, "next turn")
        assert next_assistant.get("error_info") is None
        assert any(
            part.get("type") == "text" and part.get("text") == "ok"
            for part in next_assistant["parts"]
        )
        assert "tools_called" not in next_assistant.get("metadata", {})


def test_idle_cancel_does_not_poison_next_turn(tmp_path: Path) -> None:
    """A new user turn after an idle cancellation starts normally."""

    from .conftest import complete_turn

    with _client(tmp_path) as client:
        sess = client.post("/v1/sessions", json={"title": "t"}).json()
        sid = sess["id"]
        # Cancelling an idle or restart-recovered session is a terminal transition
        # for the old work, not a reservation to cancel an unrelated future turn.
        client.post(f"/v1/sessions/{sid}/cancel")
        a = complete_turn(client, sid, "hi")
        assert a.get("error_info") is None
        assert any(p["type"] == "text" and p.get("text") == "ok" for p in a["parts"])


def test_idle_cancel_then_message_calls_agent_once(tmp_path: Path) -> None:
    """Recovery from idle cancellation forwards exactly one fresh turn."""

    from .conftest import complete_turn

    agent = _CountingAgent()
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        client.post(f"/v1/sessions/{sid}/cancel")
        assistant = complete_turn(client, sid, "run the fresh turn")

        assert agent.calls == 1
        assert assistant.get("error_info") is None
        assert any(
            part["type"] == "text" and part.get("text") == "ok" for part in assistant["parts"]
        )


def test_capabilities_advertise_foreground_executor_wire_cancellation(tmp_path: Path) -> None:
    """Global cancellation stays best-effort while foreground MCP calls support wire cancel."""

    client = _client(tmp_path)

    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_cancellation"] == "best_effort"
    assert caps["x_clio_executor_cancellation"] is True
