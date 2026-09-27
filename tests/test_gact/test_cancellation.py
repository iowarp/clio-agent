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
from tests.turn_signals import TURN_SIGNAL_BACKSTOP_S, wait_for_terminal_status

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
    # L1: the record reports what was ACTUALLY stopped -- no children/streams to
    # stop for an idle session -- never the deleted, always-constant
    # hard_abort_supported/upstream_abort/executor_work_may_continue triad.
    assert "hard_abort_supported" not in attempt
    assert "upstream_abort" not in attempt
    assert "executor_work_may_continue" not in attempt
    assert attempt["children_cancelled"] == 0
    assert attempt["provider_streams_killed"] == 0


def test_cancel_unknown_session_404s_with_v0_2_envelope(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.post("/v1/sessions/sess_nope/cancel")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "session not found" in body["error"]["message"]


class _LateToolObserverAgent:
    """Agent that reports a successful tool completion after cancellation.

    The tool "runs" until the test releases it, so its late completion provably lands
    after the cancelled envelope settled (no sleep racing the cancel route).
    """

    def __init__(self) -> None:
        import threading

        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import notify_global_tool_observer

        notify_global_tool_observer("late_tool", {"question": question}, "started", None)
        self.started.set()
        assert self.release.wait(timeout=TURN_SIGNAL_BACKSTOP_S), "the test never released"
        notify_global_tool_observer("late_tool", {"question": question}, "completed", None)
        self.completed.set()
        return type(
            "Pred", (), {"answer": "late", "selected_expert": "", "routing_rationale": ""}
        )()


class _BlocksUntilReleasedAgent:
    """Agent whose executor-thread forward runs until the test releases it."""

    def __init__(self) -> None:
        import threading

        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def forward(self, question: str, session_id: str):
        self.started.set()
        assert self.release.wait(timeout=TURN_SIGNAL_BACKSTOP_S), "the test never released"
        self.completed.set()
        return type(
            "Pred", (), {"answer": "late", "selected_expert": "", "routing_rationale": ""}
        )()


def test_cancel_during_turn_marks_turn_as_cancelled(tmp_path: Path) -> None:
    """Cancelling the asyncio task does not kill executor-thread work.

    The forward blocks until released, so the cancel always lands mid-forward and the
    envelope provably settles while the executor thread is still running (no wall-clock
    window: the old 0.6 s forward + 3 s poll raced a loaded CI runner). The settle is
    observed through the turn's own terminal ``session.status_changed``, published
    after the cancel route's ``cooperative_pending`` one.
    """

    agent = _BlocksUntilReleasedAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        # Fire turn (returns ack immediately; turn runs in background).
        c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        # Wait until the forward is actually running in the executor (the prologue
        # now runs off the loop after the ack, so a fixed slice is not enough).
        assert agent.started.wait(timeout=TURN_SIGNAL_BACKSTOP_S), "forward never started"
        assert c.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        # The route published ``cooperative_pending`` synchronously; the next terminal
        # status is the turn settling itself (after its assistant message persisted).
        after_route = app.state.bus.latest_event_id(sid)
        assert wait_for_terminal_status(app.state.bus, sid, after_event_id=after_route) == (
            "cancelled"
        )
        assert not agent.completed.is_set(), "the envelope must settle while forward runs"
        assistants = [
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "assistant" and not m.get("metadata", {}).get("live")
        ]
        assert len(assistants) == 1, assistants
        assistant = assistants[0]
        assert assistant["error_info"]["error"] == "cancelled"
        assert assistant["error_info"]["details"]["execution_cancellation"] == "best_effort"
        # L1: the deleted, always-constant triad must not reappear anywhere on the
        # settled error turn.
        assert "executor_work_may_continue" not in assistant["error_info"]["details"]
        assert "hard_abort_supported" not in assistant["error_info"]["details"]
        assert "upstream_abort" not in assistant["error_info"]["details"]
        attempt = assistant["error_info"]["details"]["cancellation_attempt"]
        assert attempt["session_id"] == sid
        assert attempt["in_flight"] is True
        assert attempt["cooperative_signal_sent"] is True
        assert attempt["asyncio_task_cancel_scheduled"] is True
        assert "executor_work_may_continue" not in attempt
        assert attempt["children_cancelled"] == 0
        assert attempt["provider_streams_killed"] == 0
        status_events = [
            e
            for e in app.state.bus._history.get(sid, [])
            if e.type == "session.status_changed" and e.payload.get("status") == "cancelled"
        ]
        assert [e.payload["execution_cancellation"] for e in status_events] == [
            "cooperative_pending",
            "best_effort",
        ]
        assert "executor_work_may_continue" not in status_events[-1].payload
        assert status_events[-1].payload["cancellation_attempt"]["id"] == attempt["id"]

        # The executor thread still finishes after the GACT envelope has truthfully
        # settled as cancelled.
        agent.release.set()
        assert agent.completed.wait(timeout=TURN_SIGNAL_BACKSTOP_S)


def test_cancel_before_turn_skips_agent_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 cancel contract: a cancel already committed before the turn's prologue
    even starts is caught by the prologue's OWN first cooperative checkpoint --
    before ``open_turn_minter``, before ``turn.started``, before anything -- and
    settles typed ``turn_cancelled_during_prologue``. This used to only be caught
    by the LATER turn-boundary check in ``turn.py`` (after the whole prologue had
    already run, per #1339's Bug 2); provider work is skipped either way."""

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
                    and message.get("error_info", {}).get("error")
                    == "turn_cancelled_during_prologue"
                ),
                None,
            )
            if assistant is not None:
                break
            _time.sleep(0.05)

        assert assistant is not None, "prologue-boundary cancellation did not settle"
        assert agent.calls == 0
        assert assistant["error_info"]["details"]["reason"] == "turn_cancelled_during_prologue"
        assert "executor_work_may_continue" not in assistant["error_info"]["details"]


def test_late_tool_completion_after_cancel_is_not_reported_as_success(
    tmp_path: Path,
) -> None:
    """Late observer completions must not become success telemetry or stale metadata."""

    from .conftest import complete_turn

    agent = _LateToolObserverAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert agent.started.wait(timeout=TURN_SIGNAL_BACKSTOP_S), "forward never started"
        assert c.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        after_route = app.state.bus.latest_event_id(sid)
        assert wait_for_terminal_status(app.state.bus, sid, after_event_id=after_route) == (
            "cancelled"
        )
        assistants = [
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "assistant" and m.get("error_info", {}).get("error") == "cancelled"
        ]
        assert len(assistants) == 1, assistants
        # Only now does the tool report success: strictly after the settle.
        agent.release.set()
        assert agent.completed.wait(timeout=TURN_SIGNAL_BACKSTOP_S)

        completed_events = [
            e
            for e in app.state.bus._history.get(sid, [])
            if e.type == "tool.call.completed" and e.payload.get("tool") == "late_tool"
        ]
        assert completed_events
        assert not any(e.payload.get("ok") is True for e in completed_events)
        assert completed_events[-1].payload["ok"] is False
        assert completed_events[-1].payload["execution_cancellation"] == "best_effort"
        assert "executor_work_may_continue" not in completed_events[-1].payload

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
