"""CLIO-BBBBBBBBBB20: POST /v1/sessions/{sid}/cancel.

Two scenarios:
- Idle session: flag flipped, status -> cancelled, event fired, 204.
- Unknown session: 404 with the v0.2 error envelope.
- Post-cancel turn: the next POST message sees the cancel flag set
  before it returns, so the turn envelope reports error=cancelled
  instead of delivering whatever the agent would have produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


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


class _CooperativeCancelAgent:
    def __init__(self) -> None:
        import threading

        self.started = threading.Event()

    def forward(
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        cancel_requested=None,
    ):
        import time

        del session_mode, session_edit_mode
        self.started.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if cancel_requested is not None and cancel_requested():
                return type(
                    "Pred",
                    (),
                    {
                        "answer": "",
                        "selected_expert": "",
                        "routing_rationale": "",
                        "error_info": {
                            "error": "cancelled",
                            "message": "turn cancelled by client",
                            "details": {
                                "execution_cancellation": "cooperative",
                                "executor_work_may_continue": False,
                                "stage": "fake_agent_loop",
                            },
                            "recoverable": True,
                        },
                    },
                )()
            time.sleep(0.02)
        return _Pred(answer="missed cancellation")


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
    assert body["error"]["error"] == "internal_error"
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


def test_cancel_reports_best_effort_for_executor_thread(tmp_path: Path) -> None:
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
        assert "tools_called" not in next_assistant.get("metadata", {})


def test_cancel_during_turn_marks_turn_as_cancelled(tmp_path: Path) -> None:
    """If /cancel fires *concurrently* with POST /messages, the turn
    resolves with error=cancelled instead of the agent's answer.

    Simulating timing is hard in a synchronous TestClient, so we
    emulate the race by setting the flag BEFORE the POST — the flag
    stays set until the handler clears it, so the order is
    observationally equivalent to a cancel landing mid-forward."""

    from .conftest import complete_turn

    client = _client(tmp_path)
    sess = client.post("/v1/sessions", json={"title": "t"}).json()
    sid = sess["id"]
    # Cancel *first*, then POST. Handler should pick up the flag.
    client.post(f"/v1/sessions/{sid}/cancel")
    a = complete_turn(client, sid, "hi")
    assert a["error_info"]["error"] == "cancelled"
    assert a["error_info"]["details"]["execution_cancellation"] == "turn_boundary"
    assert a["error_info"]["details"]["executor_work_may_continue"] is False
    # No text part with body — turn was cancelled before producing.
    parts = a["parts"]
    assert all(p["type"] != "text" or not p.get("text") for p in parts)


def test_cancel_before_turn_skips_agent_forward(tmp_path: Path) -> None:
    """A pre-set cancel flag should short-circuit before provider/tool work starts."""

    from .conftest import complete_turn

    agent = _CountingAgent()
    client = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent))
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    client.post(f"/v1/sessions/{sid}/cancel")
    assistant = complete_turn(client, sid, "this should not call the agent")

    assert agent.calls == 0
    assert assistant["error_info"]["error"] == "cancelled"
    assert assistant["error_info"]["details"]["execution_cancellation"] == "turn_boundary"
    assert assistant["error_info"]["details"]["executor_work_may_continue"] is False
    attempt = assistant["error_info"]["details"]["cancellation_attempt"]
    assert attempt["in_flight"] is False
    assert attempt["hard_abort_supported"] is False
    assert attempt["upstream_abort"] == "not_supported"


def test_agent_forward_compat_passes_cancel_callback_to_custom_agent() -> None:
    """GACT's forward shim should pass cancellation callbacks to compatible agents."""

    from clio_agent.gact.app import _agent_forward_compat

    agent = _CooperativeCancelAgent()
    pred = _agent_forward_compat(
        agent,
        "hi",
        "sess_coop",
        "chat",
        "diff",
        lambda: True,
    )

    assert pred.error_info["error"] == "cancelled"
    assert pred.error_info["details"]["execution_cancellation"] == "cooperative"
    assert pred.error_info["details"]["executor_work_may_continue"] is False


def test_capabilities_do_not_claim_hard_upstream_abort(tmp_path: Path) -> None:
    """Release contract: CLIO advertises truthful best-effort cancellation."""

    client = _client(tmp_path)

    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_cancellation"] == "best_effort"
    assert caps["x_clio_executor_cancellation"] is False
