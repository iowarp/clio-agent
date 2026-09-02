"""S1 (#948 / #662): dedicated turn runner + within-session busy gate.

Two layers:

* Direct unit tests of :class:`TurnRunner` — the master-ref set (no
  GC-cancellation), the busy signal, and the typed shutdown drain (cooperative
  settle vs forced hard-cancel).
* Integration tests through the real app — a second POST while a turn runs is a
  mid-turn steer (#1036): accepted 202, persisted as ``mid_turn_steer``, never a
  second concurrent turn (the busy gate's 409 payload survives for other
  producers) — and lifespan shutdown drains an in-flight turn deterministically.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _fire_schedule, build_app
from clio_agent.gact.turn_runner import (
    BUSY_ERROR_CODE,
    DRAIN_REASON_SERVER_SHUTDOWN,
    DrainOutcome,
    TurnRunner,
    session_busy_error_payload,
)

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

# --------------------------------------------------------------------------- #
# Direct TurnRunner unit tests (driven on a real loop via asyncio.run).        #
# --------------------------------------------------------------------------- #


def test_busy_true_while_running_and_clears_when_done() -> None:
    async def scenario() -> None:
        in_flight: dict[str, asyncio.Task] = {}
        runner = TurnRunner(in_flight)
        runner.bind_loop(asyncio.get_running_loop())
        gate = asyncio.Event()

        async def turn() -> None:
            await gate.wait()

        task = runner.spawn(turn(), sid="s1", turn_id="turn_a")
        assert runner.busy("s1") is True
        assert runner.active_count() == 1
        handle = runner.handle("s1")
        assert handle is not None and handle.turn_id == "turn_a"

        gate.set()
        await task
        # Let the done-callback run.
        await asyncio.sleep(0)
        assert runner.busy("s1") is False
        assert runner.handle("s1") is None
        assert runner.active_count() == 0
        assert in_flight.get("s1") is None

    asyncio.run(scenario())


def test_overwritten_session_slot_keeps_strong_ref_to_first_turn() -> None:
    """The GC-cancellation fix: replacing the per-session slot must NOT drop the
    first turn's last strong reference. Both turns stay tracked and run to
    completion; neither is garbage-collected mid-flight."""

    async def scenario() -> None:
        in_flight: dict[str, asyncio.Task] = {}
        runner = TurnRunner(in_flight)
        runner.bind_loop(asyncio.get_running_loop())
        gate_a = asyncio.Event()
        gate_b = asyncio.Event()
        done = {"a": False, "b": False}

        async def turn_a() -> None:
            await gate_a.wait()
            done["a"] = True

        async def turn_b() -> None:
            await gate_b.wait()
            done["b"] = True

        task_a = runner.spawn(turn_a(), sid="s1", turn_id="a")
        task_b = runner.spawn(turn_b(), sid="s1", turn_id="b")  # overwrites the slot

        # Master set holds BOTH — the first was not dropped when the slot moved.
        assert runner.active_count() == 2
        assert in_flight["s1"] is task_b  # per-session view points at the latest

        gate_a.set()
        gate_b.set()
        await asyncio.gather(task_a, task_b)
        await asyncio.sleep(0)
        assert done == {"a": True, "b": True}
        assert runner.active_count() == 0

    asyncio.run(scenario())


def test_drain_with_no_turns_is_clean_and_empty() -> None:
    async def scenario() -> None:
        runner = TurnRunner({})
        runner.bind_loop(asyncio.get_running_loop())
        outcome = await runner.drain(grace=0.05)
        assert outcome == DrainOutcome(
            total=0, settled=0, hard_cancelled=0, reason=DRAIN_REASON_SERVER_SHUTDOWN
        )
        assert outcome.clean is True

    asyncio.run(scenario())


def test_drain_settles_cooperative_turn_cleanly() -> None:
    """A turn that honours the cooperative cancel signal settles within the grace
    window — no hard cancel, outcome is clean."""

    async def scenario() -> None:
        in_flight: dict[str, asyncio.Task] = {}
        runner = TurnRunner(in_flight)
        runner.bind_loop(asyncio.get_running_loop())
        stop = {"s1": asyncio.Event()}

        async def cooperative_turn() -> None:
            await stop["s1"].wait()  # returns as soon as cancel_signal fires

        task = runner.spawn(cooperative_turn(), sid="s1", turn_id="a")

        def cancel_signal(sid: str) -> None:
            stop[sid].set()

        outcome = await runner.drain(grace=2.0, cancel_signal=cancel_signal)
        assert outcome.total == 1
        assert outcome.settled == 1
        assert outcome.hard_cancelled == 0
        assert outcome.clean is True
        assert task.done() and not task.cancelled()

    asyncio.run(scenario())


def test_drain_catches_turn_spawned_during_grace() -> None:
    """Defense-in-depth: a turn spawned DURING the drain window (modelling a
    producer that fires mid-drain) is re-snapshotted and settled too — never left
    running as teardown proceeds."""

    async def scenario() -> None:
        in_flight: dict[str, asyncio.Task] = {}
        runner = TurnRunner(in_flight)
        runner.bind_loop(asyncio.get_running_loop())
        stop = asyncio.Event()
        late: dict[str, asyncio.Task] = {}

        async def late_child() -> None:
            await asyncio.sleep(30)  # outlives the grace window

        async def turn_a() -> None:
            await stop.wait()
            # A producer firing during the drain grace window: spawn a new turn
            # AFTER drain has already snapshotted the original.
            late["task"] = runner.spawn(late_child(), sid="s2", turn_id="late")

        runner.spawn(turn_a(), sid="s1", turn_id="a")

        def cancel_signal(_sid: str) -> None:
            stop.set()

        outcome = await runner.drain(grace=0.2, cancel_signal=cancel_signal)
        assert outcome.total == 2  # both the original AND the mid-drain spawn
        assert late["task"].done()  # the late turn was caught + hard-cancelled
        assert runner.active_count() == 0

    asyncio.run(scenario())


def test_session_busy_error_payload_shape() -> None:
    """The shared 409 payload is None when idle and a typed session_busy envelope
    when a turn is in flight (so every producer refuses identically)."""

    async def scenario() -> None:
        in_flight: dict[str, asyncio.Task] = {}
        runner = TurnRunner(in_flight)
        runner.bind_loop(asyncio.get_running_loop())
        assert session_busy_error_payload(runner, "s1") is None
        assert session_busy_error_payload(None, "s1") is None

        gate = asyncio.Event()

        async def turn() -> None:
            await gate.wait()

        task = runner.spawn(turn(), sid="s1", turn_id="turn_a")
        payload = session_busy_error_payload(runner, "s1")
        assert payload is not None
        assert payload["error"]["error"] == BUSY_ERROR_CODE
        assert payload["error"]["details"]["running_turn_id"] == "turn_a"
        gate.set()
        await task

    asyncio.run(scenario())


def test_drain_hard_cancels_uncooperative_straggler() -> None:
    """A turn that ignores cooperation and outlives the grace window is
    hard-cancelled and awaited (no pending task left) — recorded, not clean."""

    async def scenario() -> None:
        in_flight: dict[str, asyncio.Task] = {}
        runner = TurnRunner(in_flight)
        runner.bind_loop(asyncio.get_running_loop())

        async def stubborn_turn() -> None:
            await asyncio.sleep(30)  # far beyond the grace window

        task = runner.spawn(stubborn_turn(), sid="s1", turn_id="a")

        outcome = await runner.drain(grace=0.1, cancel_signal=lambda _sid: None)
        assert outcome.total == 1
        assert outcome.hard_cancelled == 1
        assert outcome.settled == 0
        assert outcome.clean is False
        assert task.done() and task.cancelled()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Integration tests through the real app.                                      #
# --------------------------------------------------------------------------- #


class _SlowAgent:
    """Keeps a turn in flight long enough for a second POST / shutdown to race."""

    def __init__(self, sleep_s: float = 1.5) -> None:
        self.sleep_s = sleep_s

    def forward(self, question: str, session_id: str):
        time.sleep(self.sleep_s)
        return type(
            "Pred", (), {"answer": "done", "selected_expert": "", "routing_rationale": ""}
        )()


def _new_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "t"}).json()["id"]


def _post_message(client: TestClient, sid: str, text: str = "hi"):
    return client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": text}]},
    )


def test_second_post_while_running_steers_with_202(tmp_path: Path) -> None:
    """A busy-session steer is durable and visible before its safe boundary.

    The route returns 202 without starting a second turn. The real human message
    appears immediately with ``pending_steer`` metadata and the same identity is
    retained when the running loop consumes it.
    """

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = _new_session(client)
        first = _post_message(client, sid, "first")
        assert first.status_code == 200
        first_turn_id = first.json()["message_id"]

        _wait_busy(app, sid)
        running_task = app.state.turn_runner._in_flight.get(sid)
        assert running_task is not None

        second = _post_message(client, sid, "second")
        assert second.status_code == 202, "a mid-turn POST must be accepted as a steer, not 409'd"
        steer_id = second.json()["message_id"]

        # Acceptance is durable before the HTTP response: the actual human message
        # is already in the transcript and the inbox carries that same identity.
        by_id = {m["id"]: m for m in client.get(f"/v1/sessions/{sid}/messages").json()["messages"]}
        assert by_id[steer_id]["metadata"]["pending_steer"] is True
        assert by_id[steer_id]["metadata"]["delivery"] == "steer"
        assert app.state.loop_inboxes[sid].peek_nonempty(), (
            "the steer was not buffered for the drain"
        )

        # It did NOT start a second turn: the running turn still owns the slot and
        # its id is unchanged.
        assert app.state.turn_runner._in_flight.get(sid) is running_task, (
            "the steer orphaned/replaced the running turn's slot"
        )
        assert app.state.turn_runner.handle(sid).turn_id == first_turn_id

        # The busy-gate 409 payload is still available for the producers that keep it.
        payload = session_busy_error_payload(app.state.turn_runner, sid)
        assert payload is not None and payload["error"]["error"] == BUSY_ERROR_CODE


def test_three_accepted_steers_keep_three_identities_through_idle_boundaries(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.4))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post_message(client, sid, "first").status_code == 200
        _wait_busy(app, sid)

        accepted_ids: list[str] = []
        for index in range(3):
            message_id = f"msg_steer_{index}"
            response = client.post(
                f"/v1/sessions/{sid}/messages",
                json={
                    "client_message_id": message_id,
                    "idempotency_key": f"steer-{index}",
                    "delivery": "steer",
                    "parts": [{"type": "text", "text": f"steer {index}"}],
                },
            )
            assert response.status_code == 202, response.text
            assert response.json()["message_id"] == message_id
            accepted_ids.append(message_id)

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            pending = client.get(f"/v1/sessions/{sid}/pending-steers").json()["pending_steers"]
            if not pending and not app.state.turn_runner.busy(sid):
                break
            time.sleep(0.05)
        assert pending == []

        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        by_id = {row["id"]: row for row in messages}
        assert set(accepted_ids) <= set(by_id)
        for message_id in accepted_ids:
            assert by_id[message_id]["metadata"]["pending_steer"] is False
            assert by_id[message_id]["metadata"]["mid_turn_steer"] is True


def _wait_busy(app, sid: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not app.state.turn_runner.busy(sid):
        time.sleep(0.02)
    assert app.state.turn_runner.busy(sid), "turn never went in flight"


def _user_texts(client: TestClient, sid: str) -> list[str]:
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    return [
        "".join(p.get("text", "") for p in m.get("parts", [])) for m in msgs if m["role"] == "user"
    ]


def test_scheduler_defers_busy_session(tmp_path: Path) -> None:
    """A due schedule whose session already has a turn in flight is NOT
    double-staged; its id is recorded in the deferred set for retry (the busy gate
    covers the scheduler producer, not just POST /messages)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post_message(client, sid, "first").status_code == 200
        _wait_busy(app, sid)

        sch = app.state.schedules.add(session_id=sid, cron="* * * * *", question="scheduled q")
        _fire_schedule(app, sch)  # busy -> defer, not stage

        assert _user_texts(client, sid) == ["first"], "scheduler double-staged onto a busy session"
        assert sch.id in app.state.deferred_schedules, "busy schedule was not deferred for retry"


def test_scheduler_deferred_retry_fires_when_session_frees(tmp_path: Path) -> None:
    """A deferred (coarse-cron) occurrence is retried and fired once the session
    frees — the tick's deferred pass does NOT rely on due_now re-yielding, which a
    coarse cron would never do."""

    from clio_agent.gact.app import _scheduler_tick_once

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=0.4))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post_message(client, sid, "first").status_code == 200
        _wait_busy(app, sid)
        # A coarse cron that would NOT re-match the next minute.
        sch = app.state.schedules.add(session_id=sid, cron="0 9 * * *", question="daily q")
        _fire_schedule(app, sch)
        assert sch.id in app.state.deferred_schedules

        # Let the running turn finish, then run a tick: the deferred pass fires it.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and app.state.turn_runner.busy(sid):
            time.sleep(0.05)
        assert not app.state.turn_runner.busy(sid)
        _scheduler_tick_once(app)

        assert sch.id not in app.state.deferred_schedules, "deferred schedule was not retried"
        # The scheduled occurrence actually ran (question staged) once free.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and "daily q" not in _user_texts(client, sid):
            time.sleep(0.05)
        assert "daily q" in _user_texts(client, sid), "deferred occurrence never fired when free"


def _bus_events(app, sid: str, event_type: str) -> list:
    return [e for e in app.state.bus._history.get(sid, []) if e.type == event_type]


def test_answer_resume_deferred_when_busy_then_redriven(tmp_path: Path) -> None:
    """#1036: answering a stale pending question while an intervening turn runs must
    NOT double-stage a concurrent resume (orphaning the running one) — but it must
    also NOT drop the answer. The resume is folded into the loop inbox as a
    user_message steer (typed resume_deferred event), and the idle hook re-drives it
    into exactly ONE new turn the instant the session frees."""

    from clio_agent.gact.loop_inbox import inbox_for
    from clio_agent.gact.types import UserQuestion

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.0))
    with TestClient(app) as client:
        sid = _new_session(client)
        q = UserQuestion(
            id="q_stale",
            session_id=sid,
            turn_id="turn_paused",
            prompt="continue?",
            options=[],
            status="pending",
            metadata={"resume_on_answer": True},
            created_at="2026-07-17T00:00:00+00:00",
            updated_at="2026-07-17T00:00:00+00:00",
        )
        app.state.user_questions[q.id] = q

        # An intervening turn is now in flight on the same session.
        assert _post_message(client, sid, "intervening").status_code == 200
        _wait_busy(app, sid)

        resp = client.post(f"/v1/sessions/{sid}/questions/{q.id}/answer", json={"answer": "yes"})
        assert resp.status_code == 200
        # Folded, not double-staged, not dropped: only the intervening turn is staged
        # so far, the resume rides the inbox as a steer, and a typed event fired.
        assert _user_texts(client, sid) == ["intervening"], (
            "resume double-staged onto a busy session"
        )
        assert inbox_for(app, sid).peek_nonempty(), "resume was dropped, not enqueued as a steer"
        assert _bus_events(app, sid, "user_question.resume_deferred"), "no typed deferral event"

        # Once the intervening turn finishes, the idle hook re-drives the resume as
        # one new turn and the inbox drains to empty.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and inbox_for(app, sid).peek_nonempty():
            time.sleep(0.05)
        assert not inbox_for(app, sid).peek_nonempty(), "buffered resume steer was never re-driven"

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(_user_texts(client, sid)) < 2:
            time.sleep(0.05)
        assert len(_user_texts(client, sid)) >= 2, (
            "resume turn never staged after the session freed"
        )
        assert _bus_events(app, sid, "user_question.resumed"), "no user_question.resumed event"


def test_retry_execute_while_busy_is_blocked(tmp_path: Path) -> None:
    """A retry-execute while a turn is already in flight records the attempt with a
    typed session_busy blocked reason instead of double-staging a concurrent turn."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=1.5))
    with TestClient(app) as client:
        sid = _new_session(client)
        first = _post_message(client, sid, "first")
        first_id = first.json()["message_id"]
        _wait_busy(app, sid)

        resp = client.post(
            f"/v1/sessions/{sid}/messages/{first_id}/retry",
            json={"execute": True, "notes": ""},
        )
        assert resp.status_code == 202
        attempt = resp.json()
        assert attempt["metadata"]["execution_blocked_reason"] == "session_busy"
        assert "queued_user_message_id" not in attempt["metadata"], "retry double-staged while busy"


def test_shutdown_does_not_redrive_deferred_resume(tmp_path: Path) -> None:
    """#1036: shutdown must NOT re-drive a buffered resume steer: a draining turn's
    completion would otherwise stage a fresh resume whose task is hard-cancelled but
    whose side effects (a misleading user_question.resumed event, a stuck-running
    session) survive. The idle hook is unregistered before the drain."""

    from clio_agent.gact.loop_inbox import enqueue_user_steer

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=10.0))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post_message(client, sid, "intervening").status_code == 200
        _wait_busy(app, sid)
        # A resume steer is buffered (as answer-while-busy would enqueue it).
        enqueue_user_steer(app, sid, "resume now", {"ask_user_resume": True, "question_id": "q1"})
    # Context exit ran the lifespan shutdown (drains the in-flight turn). The
    # turn's _done must NOT fire the resume re-drive.
    resumed = [e for e in app.state.bus._history.get(sid, []) if e.type == "user_question.resumed"]
    assert not resumed, "shutdown drain misleadingly re-drove a buffered resume steer"


def test_shutdown_drains_in_flight_turn(tmp_path: Path) -> None:
    """Lifespan shutdown settles an in-flight turn deterministically — the drain
    outcome records it and no task is left pending."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_SlowAgent(sleep_s=30.0))
    with TestClient(app) as client:
        sid = _new_session(client)
        assert _post_message(client, sid, "hi").status_code == 200
        # Ensure the turn is actually in flight before we tear down.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not app.state.turn_runner.busy(sid):
            time.sleep(0.02)
        assert app.state.turn_runner.busy(sid) is True
    # TestClient context exit ran the lifespan shutdown → drain.
    outcome = app.state.turn_drain_outcome
    assert outcome.total >= 1
    assert outcome.reason == DRAIN_REASON_SERVER_SHUTDOWN
    # Every in-flight turn was accounted for (settled or hard-cancelled) — none
    # left pending.
    assert outcome.settled + outcome.hard_cancelled == outcome.total
    assert app.state.turn_runner.active_count() == 0
