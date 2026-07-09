"""Regression tests for iowarp/clio-agent#761.

Two related event-bus defects:

1. Replay pollution: the SSE route publishes a ``server.heartbeat``
   every 15s per subscriber, and each one was recorded into the
   256-slot replay buffer — an idle hour evicts every real event, so
   ``Last-Event-ID`` resume silently gaps. Heartbeats are connection
   keepalive, not session timeline: they must be live-delivery only.

2. Watchdog fold: every publish also stamped the global ``""``
   progress key and the turn watchdog took ``max(sid, "")`` — with
   two active sessions, session B's events kept a genuinely wedged
   session A "alive" forever. Progress attribution must be
   per-session.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event, EventBus, heartbeat_event

# ---- replay pollution (bus-level) ------------------------------------------


@pytest.mark.asyncio
async def test_replay_survives_heartbeat_flood() -> None:
    """Heartbeats must not evict real events from the replay buffer.

    Floods more heartbeats than the replay buffer holds (an idle
    ~75 minutes at the 15s cadence), exactly as the SSE route
    publishes them, then resumes: the real event must still replay
    and no heartbeat may be replayed.
    """

    bus = EventBus()  # default 256-slot history
    real = Event(type="message.created", session_id="s1", payload={"real": True})
    bus.publish(real)
    for _ in range(300):
        bus.publish(heartbeat_event("s1"))

    replayed: list[Event] = []
    sub = bus.subscribe("s1")
    async for ev in sub:
        replayed.append(ev)
        if ev.id >= real.id:
            await sub.aclose()
            break

    types = [ev.type for ev in replayed]
    assert "message.created" in types
    assert "server.heartbeat" not in types


@pytest.mark.asyncio
async def test_heartbeats_still_reach_live_subscribers() -> None:
    """Bypassing history must not break live keepalive delivery."""

    import asyncio

    bus = EventBus()
    received: list[str] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s1"):
            received.append(ev.type)
            break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    bus.publish(heartbeat_event("s1"))
    await asyncio.wait_for(task, timeout=2.0)

    assert received == ["server.heartbeat"]


# ---- watchdog progress attribution (bus-level) ------------------------------


def test_publish_does_not_stamp_global_or_foreign_session_progress() -> None:
    """Session B's events must not register as progress for session A.

    The turn watchdog keys progress off the turn's own session; a
    global ``""`` fold (any session's publish refreshing every
    watchdog) disables the only guardrail against wedged turns as
    soon as a second session is active.
    """

    bus = EventBus()
    bus.publish(Event(type="semantic.event", session_id="B", payload={}))

    assert bus.last_publish_monotonic("B") > 0.0
    assert bus.last_publish_monotonic("A") == 0.0
    assert bus.last_publish_monotonic("") == 0.0


def test_heartbeats_do_not_count_as_watchdog_progress() -> None:
    """Keepalives fire every 15s for any attached SSE client; if they
    stamped progress, a wedged session with a connected client would
    never time out."""

    bus = EventBus()
    bus.publish(heartbeat_event("s1"))

    assert bus.last_publish_monotonic("s1") == 0.0


# ---- watchdog end-to-end: wedged session + busy neighbor --------------------


class _FakePrediction:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.selected_expert = "none"
        self.routing_rationale = ""


class _WedgedAgent:
    """Publishes nothing and sleeps past the no-progress window."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str) -> Any:
        self.calls.append((question, session_id))
        time.sleep(self.delay_s)
        return _FakePrediction("too late")


def test_wedged_session_times_out_while_another_session_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged turn must hit the no-progress timeout even while a
    second session keeps publishing events."""

    from .conftest import complete_turn

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "0.2")
    agent = _WedgedAgent(delay_s=2.0)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    with TestClient(app) as client:
        wedged_sid = client.post("/v1/sessions", json={"title": "wedged"}).json()["id"]
        busy_sid = client.post("/v1/sessions", json={"title": "busy"}).json()["id"]

        stop = threading.Event()

        def busy_neighbor() -> None:
            while not stop.is_set():
                app.state.bus.publish(
                    Event(
                        type="semantic.event",
                        session_id=busy_sid,
                        payload={"summary": "busy neighbor progress"},
                    )
                )
                time.sleep(0.05)

        neighbor = threading.Thread(target=busy_neighbor, name="busy-neighbor")
        neighbor.start()
        try:
            assistant = complete_turn(client, wedged_sid, "hi", timeout=5.0)
        finally:
            stop.set()
            neighbor.join(timeout=2.0)

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "provider_timeout"


def test_wedged_session_times_out_while_neighbor_holds_lm_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual of #761 defect (2): the LM-inflight liveness net must be
    per-session too, not process-global.

    The watchdog treats an *actively generating* LM call as progress
    (a deep-reasoning model streams its chain-of-thought with no bus
    events). If that signal is process-global, a genuinely wedged
    session A is kept alive forever by a neighbor session B that holds
    a live LM call — the exact 'guardrail disabled when it matters'
    shape of #761, just narrowed to the LM-generating case.

    Here session A is wedged (its fake agent makes no LM call and just
    sleeps past the window) while a neighbor thread simulates session
    B's long LM call: ``note_lm_start`` + periodic ``note_lm_activity``
    under session B's context, never ``note_lm_end`` until stop, and
    no bus publishes at all. With per-session LM attribution, A's
    watchdog must ignore B's in-flight call and still time out.
    """

    from clio_agent.gact import context as gact_ctx
    from clio_agent.runtime import lm_activity

    from .conftest import complete_turn

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "0.2")
    agent = _WedgedAgent(delay_s=2.0)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    with TestClient(app) as client:
        wedged_sid = client.post("/v1/sessions", json={"title": "wedged"}).json()["id"]
        busy_sid = client.post("/v1/sessions", json={"title": "busy"}).json()["id"]

        stop = threading.Event()

        def neighbor_lm_call() -> None:
            # Attribute the in-flight LM call to session B, exactly as the
            # note_lm_* callbacks do inside session B's turn/executor context.
            gact_ctx.set_session_id(busy_sid)
            lm_activity.note_lm_start()
            try:
                while not stop.is_set():
                    lm_activity.note_lm_activity()  # steady tokens: never idles out
                    time.sleep(0.05)
            finally:
                # Always release the global/session inflight state so it can't
                # leak into other tests.
                lm_activity.note_lm_end()

        neighbor = threading.Thread(target=neighbor_lm_call, name="neighbor-lm")
        neighbor.start()
        try:
            assistant = complete_turn(client, wedged_sid, "hi", timeout=5.0)
        finally:
            stop.set()
            neighbor.join(timeout=2.0)

    assert assistant["stop_reason"] == "error"
    assert assistant["error_info"]["error"] == "provider_timeout"
