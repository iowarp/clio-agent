"""A finishing turn's terminal status never runs ahead of the busy gate.

Regression for the ``test_second_turn_does_not_reopen_bringup`` flake: finalize
flipped the session to ``idle`` (and published ``session.status_changed``) while
the turn task was still running its tail (Stop / loop hooks, the awaited GOAL
judge). A client that waited for ``idle`` and then posted got ``delivery=steer``
/ ``pending_steer`` (202) because the ``TurnRunner`` slot was still held.

Two layers:

* :meth:`TurnRunner.run_when_released` unit tests — immediate when idle,
  deferred to the done-callback otherwise, fired after the slot clears and
  before the idle hook, and isolated from a failing callback.
* Through the real app — the finalize tail is held open; for its whole duration
  ``GET /v1/sessions/{sid}`` still says ``running``, the ``idle`` event is
  published only once ``busy()`` is false, and the next POST starts a turn.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.turn_runner import TurnRunner
from clio_agent.gact.turn_settle_status import publish_turn_settled_status

from .test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# --------------------------------------------------------------------------- #
# TurnRunner.run_when_released                                                 #
# --------------------------------------------------------------------------- #


def test_run_when_released_runs_immediately_when_no_turn_in_flight() -> None:
    runner = TurnRunner({})
    calls: list[str] = []

    runner.run_when_released("s1", lambda: calls.append("settled"))

    assert calls == ["settled"]


def test_run_when_released_defers_until_slot_clears_and_precedes_idle_hook() -> None:
    async def scenario() -> list[tuple[str, bool]]:
        runner = TurnRunner({})
        runner.bind_loop(asyncio.get_running_loop())
        order: list[tuple[str, bool]] = []
        runner.set_idle_hook(lambda sid: order.append(("idle_hook", runner.busy(sid))))
        gate = asyncio.Event()

        async def turn() -> None:
            # Registered from inside the turn, like finalize does.
            runner.run_when_released("s1", lambda: order.append(("settle", runner.busy("s1"))))
            order.append(("registered", runner.busy("s1")))
            await gate.wait()

        task = runner.spawn(turn(), sid="s1", turn_id="turn_a")
        await asyncio.sleep(0)
        assert order == [("registered", True)]  # not run while the slot is held

        gate.set()
        await task
        await asyncio.sleep(0)
        return order

    order = asyncio.run(scenario())
    # Sabotage: run the callback at registration (or after the idle hook) -> the
    # settle entry moves / reports busy=True -> red.
    assert order == [("registered", True), ("settle", False), ("idle_hook", False)]


def test_run_when_released_registered_from_executor_thread() -> None:
    """Finalize registers from the turn executor; the callback still fires on release."""

    async def scenario() -> list[bool]:
        runner = TurnRunner({})
        runner.bind_loop(asyncio.get_running_loop())
        seen: list[bool] = []

        async def turn() -> None:
            await asyncio.to_thread(
                runner.run_when_released, "s1", lambda: seen.append(runner.busy("s1"))
            )
            assert seen == []

        await runner.spawn(turn(), sid="s1", turn_id="turn_a")
        await asyncio.sleep(0)
        return seen

    assert asyncio.run(scenario()) == [False]


def test_failing_release_callback_does_not_block_others_or_idle_hook() -> None:
    async def scenario() -> list[str]:
        runner = TurnRunner({})
        runner.bind_loop(asyncio.get_running_loop())
        order: list[str] = []
        runner.set_idle_hook(lambda _sid: order.append("idle_hook"))

        def boom() -> None:
            raise RuntimeError("settle failed")

        async def turn() -> None:
            runner.run_when_released("s1", boom)
            runner.run_when_released("s1", lambda: order.append("second"))

        await runner.spawn(turn(), sid="s1", turn_id="turn_a")
        await asyncio.sleep(0)
        return order

    assert asyncio.run(scenario()) == ["second", "idle_hook"]


# --------------------------------------------------------------------------- #
# publish_turn_settled_status                                                  #
# --------------------------------------------------------------------------- #


class _App:
    def __init__(self, tmp_path: Path) -> None:
        self.state = type("S", (), {})()
        self.state.sessions = SessionStore(tmp_path / "s.json")
        self.state.published = []
        self.state.bus = type("B", (), {"publish": self.state.published.append})()
        self.state.turn_runner = TurnRunner({})


def test_settled_status_publishes_status_and_event(tmp_path: Path) -> None:
    app = _App(tmp_path)
    sid = app.state.sessions.create(workspace_id="ws", title="t").id
    app.state.sessions.update(sid, status="running")

    publish_turn_settled_status(app, sid, "error", payload_extra={"reason": "x"})  # type: ignore[arg-type]

    assert app.state.sessions.get(sid).status == "error"
    [event] = app.state.published
    assert isinstance(event, Event)
    assert event.type == "session.status_changed"
    assert event.payload == {
        "session_id": sid,
        "status": "error",
        "prev_status": "running",
        "reason": "x",
    }


def test_settled_status_superseded_by_a_later_transition_is_dropped(tmp_path: Path) -> None:
    """A status another writer set during the turn's tail (e.g. POST /cancel) stands."""

    async def scenario() -> None:
        app = _App(tmp_path)
        runner = app.state.turn_runner
        runner.bind_loop(asyncio.get_running_loop())
        sid = app.state.sessions.create(workspace_id="ws", title="t").id
        app.state.sessions.update(sid, status="running")
        gate = asyncio.Event()

        async def turn() -> None:
            publish_turn_settled_status(app, sid, "idle")  # type: ignore[arg-type]
            await gate.wait()

        task = runner.spawn(turn(), sid=sid, turn_id="turn_a")
        await asyncio.sleep(0)
        app.state.sessions.update(sid, status="cancelled")  # the cancel route, mid-tail
        gate.set()
        await task
        await asyncio.sleep(0)
        assert app.state.sessions.get(sid).status == "cancelled"
        assert app.state.published == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Through the real app                                                         #
# --------------------------------------------------------------------------- #


def _wait_status_not_running(c: TestClient, sid: str) -> str:
    deadline = time.monotonic() + 10.0
    status = "running"
    while time.monotonic() < deadline:
        status = c.get(f"/v1/sessions/{sid}").json()["status"]
        if status != "running":
            break
        time.sleep(0.02)
    return status


def test_idle_is_not_observable_while_the_finalize_tail_holds_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tail_entered = threading.Event()
    release_tail = threading.Event()

    async def held_goal_step(*_args: Any, **_kwargs: Any) -> None:
        # The GOAL judge is the last awaited step of the turn's finalize tail.
        tail_entered.set()
        await asyncio.to_thread(release_tail.wait, 10.0)
        return None

    monkeypatch.setattr(
        "clio_agent.gact.turn_finalize_goal.dispatch_goal_at_finalize", held_goal_step
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="done"))

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "settle"}).json()["id"]
        runner = app.state.turn_runner
        idle_publishes: list[bool] = []
        original_publish = app.state.bus.publish

        def recording_publish(event: Event) -> Any:
            if (
                event.type == "session.status_changed"
                and event.session_id == sid
                and event.payload.get("status") == "idle"
            ):
                idle_publishes.append(runner.busy(sid))
            return original_publish(event)

        monkeypatch.setattr(app.state.bus, "publish", recording_publish)

        ack = c.post(
            f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "one"}]}
        )
        assert ack.status_code == 200, ack.text
        assert tail_entered.wait(10.0), "finalize tail never reached"

        # The assistant message is complete but the turn still holds its slot.
        # Sabotage: flip the status inside finalize again -> "idle" here -> red.
        assert runner.busy(sid) is True
        assert c.get(f"/v1/sessions/{sid}").json()["status"] == "running"
        assert idle_publishes == []

        release_tail.set()
        assert _wait_status_not_running(c, sid) == "idle"
        # The idle event went out only once the busy gate was open.
        assert idle_publishes == [False]

        second = c.post(
            f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "two"}]}
        )
        assert second.status_code == 200, second.text
        assert second.json()["delivery"] == "start"
        assert _wait_status_not_running(c, sid) == "idle"
