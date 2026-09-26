"""#756: an exception in the turn finalize region must settle the turn.

Everything after ``_run_turn_in_background``'s forward except-chain (answer
grounding, part assembly, diff indexing, publishes, persistence) runs inside a
fire-and-forget task. Before the fix, an exception there died silently: no
``message.completed``, no ``session.status_changed``, session wedged in
``running`` forever. The finalize region is now wrapped in the turn's error
envelope, so an injected finalize exception must yield a visible error turn
and a terminal session status.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to the
# ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

from .test_post_messages import FakeClioAgent


def test_finalize_exception_settles_turn_with_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalize-only helper raising must not wedge the session in running."""

    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    # _enrich_cancellation_error_info runs unconditionally in the finalize
    # region (after the forward except-chain), so raising here simulates any
    # finalize crash: grounding, Part construction, Pydantic validation, ...
    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text
        user_id = ack.json()["message_id"]

        # Poll until the session leaves 'running' (or time out — the pre-fix
        # symptom: the background task dies silently and the status never
        # changes, so this loop exhausts and the assert below reports it).
        deadline = time.monotonic() + 5.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)

        history = app.state.bus._history.get(sid, [])
        completed = [ev for ev in history if ev.type == "message.completed"]

        assert status == "error", (
            f"finalize exception must settle the session to a terminal status; "
            f"session stayed {status!r} with "
            f"{len(completed)} message.completed event(s)"
        )

        # The failure is a visible error turn on the bus...
        assert completed, "finalize exception must still publish message.completed"
        payload = completed[-1].payload
        assert payload["turn_id"] == user_id
        assert payload["stop_reason"] == "error"
        assert payload["error_info"]["error"] == "finalize_error"
        assert "simulated finalize failure" in payload["error_info"]["message"]
        assert payload["error_info"]["details"]["reason"] == "turn_finalize_error"

        status_events = [ev for ev in history if ev.type == "session.status_changed"]
        assert status_events and status_events[-1].payload["status"] == "error"

        # ...and in the persisted transcript.
        msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        error_turns = [m for m in msgs if m["role"] == "assistant" and m.get("turn_id") == user_id]
        assert error_turns, "error turn must land in the persisted transcript"
        assert error_turns[0]["stop_reason"] == "error"


def test_finalize_survives_a_prologue_that_skipped_context_file_provenance(
    tmp_path: Path,
) -> None:
    """#1331 review round: a turn whose prologue never ran
    ``turn_start_offloop.prepare_turn_off_loop`` (both of its
    ``state.context_file_provenance = ...`` assignments) must still finalize
    cleanly, not crash with ``TypeError: 'NoneType' object is not subscriptable``
    at ``turn_finalize.py``'s ``state.context_file_provenance["files"]``.

    Drives ``finalize_turn`` directly on a ``TurnState`` built via
    ``new_turn_state`` (which does NOT touch ``context_file_provenance`` --
    only the prologue does) with the other ``init=False`` fields
    (``workflow_schema``/``transcript``/``turn_cancel_event``) wired the same
    way ``turn.py``'s own linear body wires them, BEFORE the try block that
    calls ``prepare_turn_off_loop`` -- reproducing exactly the real ordering
    gap: a turn that reaches finalize without its off-loop prologue ever
    running (e.g. a crash/cancellation between that setup and the prologue
    call) still carries an initialized identity but an unset
    ``context_file_provenance``.
    """

    from clio_agent.gact.agents.resolution import _active_workflow_state_schema
    from clio_agent.gact.tool_observer import _open_turn_transcript
    from clio_agent.gact.turn_finalize import finalize_turn
    from clio_agent.gact.turn_state import new_turn_state
    from clio_agent.gact.turn_watchdog import make_turn_cancel_event
    from clio_agent.gact.types import Message, Part

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    now = "2026-09-12T00:00:00+00:00"
    user_msg = Message(
        id="msg_user_1",
        session_id=sid,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_user_1", type="text", text="hello")],
    )
    sess = app.state.sessions.get(sid)
    state = new_turn_state(app, sid, "hello", user_msg, "main", sess=sess, bus=app.state.bus)
    # Wire the other init=False fields exactly as turn.py's linear body does,
    # BEFORE the try block that calls prepare_turn_off_loop -- so this state
    # is otherwise fully turn-shaped, only missing the ONE prologue-only
    # assignment under test.
    state.workflow_schema = _active_workflow_state_schema(app, sid)
    state.transcript = _open_turn_transcript(app, sid, state.turn_id)
    make_turn_cancel_event(state)
    # NEVER call prepare_turn_off_loop: context_file_provenance stays at
    # TurnState's own default. context_frame shares the same None-until-
    # prologue shape (also only set by prepare_turn_off_loop, via
    # _record_context_frame) and finalize_turn subscripts it too
    # (state.context_frame["id"]) -- out of THIS fix's scope, so stand in a
    # minimal real value here to isolate the one field under test.
    state.context_frame = {"id": "frame-test"}
    state.answer_text = "a real answer, so this isn't the empty_response branch"

    pred = object()  # every read of it uses getattr(..., default) or is guarded
    finalize_turn(
        state,
        pred,
        drain_observed_tool_calls=lambda calls: calls,
        update_retry_attempt=lambda *args, **kwargs: None,
    )  # must not raise

    assert state.context_file_provenance == {
        "status": "unset",
        "count": 0,
        "max_inline_bytes": state.context_file_provenance["max_inline_bytes"],
        "files": [],
    }
    assert "context_files" not in state.assistant_metadata


def _new_prologue_shaped_state(
    app: Any,
    sid: str,
    turn_msg_id: str,
) -> Any:
    """Build a ``TurnState`` wired exactly as ``turn.py``'s linear body wires the
    ``init=False`` infra fields BEFORE the try block that calls
    ``prepare_turn_off_loop`` -- then never call it, so every prologue-derived
    field (``context_frame``, ``context_file_provenance``, ``enriched_text``,
    ``memory_search_metadata``) stays at ``TurnState``'s own default and
    ``prologue_phase`` stays ``"not_started"``. Shared by the #1339 round-5 /
    L1 tests below (the guard, and the crash it prevents)."""

    from clio_agent.gact.agents.resolution import _active_workflow_state_schema
    from clio_agent.gact.tool_observer import _open_turn_transcript
    from clio_agent.gact.turn_state import new_turn_state
    from clio_agent.gact.turn_watchdog import make_turn_cancel_event
    from clio_agent.gact.types import Message, Part

    now = "2026-09-13T00:00:00+00:00"
    user_msg = Message(
        id=turn_msg_id,
        session_id=sid,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id=f"part_{turn_msg_id}", type="text", text="hello")],
    )
    sess = app.state.sessions.get(sid)
    state = new_turn_state(app, sid, "hello", user_msg, "main", sess=sess, bus=app.state.bus)
    state.workflow_schema = _active_workflow_state_schema(app, sid)
    state.transcript = _open_turn_transcript(app, sid, state.turn_id)
    make_turn_cancel_event(state)
    return state


async def test_prologue_never_ran_settles_typed_instead_of_crashing_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1339 round 5 / L1: a turn whose off-loop prologue (``prepare_turn_off_loop``)
    never ran at all -- EVERY prologue-derived field left at ``TurnState``'s own
    default, ``prologue_phase`` still ``"not_started"`` -- must settle a typed
    ``turn_prologue_never_ran`` error turn through
    ``turn_prologue_guard.run_finalize_or_settle_prologue_gap``, which must never
    call ``finalize_turn`` for such a turn, and must never crash.

    See ``test_finalize_turn_crashes_on_an_unset_context_frame_without_the_guard``
    below for the reverted-fix reproduction the coordinator's CI trace named
    (``state.context_frame["id"]`` -> ``TypeError``): this test only proves the
    guard's OWN behavior, not the absence of a guard.
    """

    import clio_agent.gact.turn_prologue_guard as guard_module
    from clio_agent.gact.turn_prologue_guard import (
        REASON_PROLOGUE_NEVER_RAN,
        run_finalize_or_settle_prologue_gap,
    )

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        guard_module,
        "stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    app.state.sessions.update(sid, status="running")
    state = _new_prologue_shaped_state(app, sid, "msg_user_1")

    assert state.prologue_phase == "not_started"
    assert state.context_frame is None
    assert state.context_file_provenance["status"] == "unset"

    await run_finalize_or_settle_prologue_gap(
        state,
        drain_observed_tool_calls=lambda calls: calls,
        update_retry_attempt=lambda *args, **kwargs: None,
    )  # must not raise

    # Neither prologue-derived field was ever touched by finalize.
    assert state.prologue_phase == "not_started"
    assert state.context_frame is None

    reasons = [f for stage, f in audits if stage == guard_module.AUDIT_PROLOGUE_NEVER_RAN]
    assert reasons, audits
    assert reasons[-1]["reason"] == REASON_PROLOGUE_NEVER_RAN
    assert reasons[-1]["session_id"] == sid
    assert reasons[-1]["turn_id"] == state.turn_id

    history = app.state.bus._history.get(sid, [])
    completed = [ev for ev in history if ev.type == "message.completed"]
    assert completed, "a prologue-never-ran turn must still publish message.completed"
    payload = completed[-1].payload
    assert payload["stop_reason"] == "error"
    assert payload["error_info"]["error"] == REASON_PROLOGUE_NEVER_RAN
    assert payload["error_info"]["details"]["reason"] == REASON_PROLOGUE_NEVER_RAN

    status_events = [ev for ev in history if ev.type == "session.status_changed"]
    assert status_events and status_events[-1].payload["status"] == "error"
    assert app.state.sessions.get(sid).status == "error"


async def test_prologue_exception_settles_with_real_cause_not_never_ran(
    tmp_path: Path,
) -> None:
    """L1: an exception raised INSIDE the prologue (the deferred transcript job
    failing its ARC persist, e.g. ``TranscriptIngestError``) must settle the turn
    with the REAL exception's cause/type/message, never the fabricated
    ``turn_prologue_never_ran`` a bare completed/not-completed bit used to produce."""

    from clio_agent.gact.transcript_projection import TranscriptIngestError
    from clio_agent.gact.turn_prologue_guard import (
        REASON_PROLOGUE_NEVER_RAN,
        run_finalize_or_settle_prologue_gap,
    )
    from clio_agent.gact.turn_start_offloop import prepare_turn_off_loop
    from clio_agent.gact.turn_state import DeferredTranscriptJob

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    app.state.sessions.update(sid, status="running")
    state = _new_prologue_shaped_state(app, sid, "msg_user_boom")

    cause = TranscriptIngestError(sid, "msg_user_boom", RuntimeError("disk full"))

    def _boom() -> None:
        raise cause

    state.transcript_job = DeferredTranscriptJob(_boom)

    with pytest.raises(TranscriptIngestError):
        prepare_turn_off_loop(state, update_retry_attempt=lambda *a, **k: None)

    # The prologue DID start (it opened the minter and claimed the job) and then
    # crashed -- "failed", never "not_started" -- carrying the REAL exception.
    assert state.prologue_phase == "failed"
    assert state.prologue_error is cause

    await run_finalize_or_settle_prologue_gap(
        state,
        drain_observed_tool_calls=lambda calls: calls,
        update_retry_attempt=lambda *args, **kwargs: None,
    )  # must not raise

    history = app.state.bus._history.get(sid, [])
    completed = [ev for ev in history if ev.type == "message.completed"]
    assert completed, "a real prologue crash must still publish message.completed"
    error_info = completed[-1].payload["error_info"]
    assert error_info["error"] != REASON_PROLOGUE_NEVER_RAN
    assert error_info["details"]["reason"] != REASON_PROLOGUE_NEVER_RAN
    assert error_info["details"]["original_error"] == "TranscriptIngestError"
    assert "disk full" in error_info["message"]
    assert "transcript_ingest_failed" in error_info["message"]
    assert app.state.sessions.get(sid).status == "error"


async def test_orphaned_transcript_job_settles_as_never_ran(tmp_path: Path) -> None:
    """L1: the ONE real 'never started' proof -- an unclaimed ``DeferredTranscriptJob``
    (the same fact ``turn_start_offloop.spawn_user_turn``'s done-callback checks) --
    must settle typed ``turn_prologue_never_ran`` when ``prepare_turn_off_loop`` is
    never even invoked (``prologue_phase`` stays its default, ``"not_started"``)."""

    from clio_agent.gact.turn_prologue_guard import (
        REASON_PROLOGUE_NEVER_RAN,
        run_finalize_or_settle_prologue_gap,
    )
    from clio_agent.gact.turn_state import PROLOGUE_NOT_STARTED, DeferredTranscriptJob

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    app.state.sessions.update(sid, status="running")
    state = _new_prologue_shaped_state(app, sid, "msg_user_orphan")

    claimed: list[int] = []
    state.transcript_job = DeferredTranscriptJob(lambda: claimed.append(1))
    assert state.prologue_phase == PROLOGUE_NOT_STARTED  # prepare_turn_off_loop never ran

    await run_finalize_or_settle_prologue_gap(
        state,
        drain_observed_tool_calls=lambda calls: calls,
        update_retry_attempt=lambda *args, **kwargs: None,
    )

    # The job is still there for the orphan flush to claim -- proof it never ran.
    leftover = state.transcript_job.take()
    assert leftover is not None
    assert claimed == []

    history = app.state.bus._history.get(sid, [])
    completed = [ev for ev in history if ev.type == "message.completed"]
    assert completed
    error_info = completed[-1].payload["error_info"]
    assert error_info["error"] == REASON_PROLOGUE_NEVER_RAN
    assert error_info["details"]["reason"] == REASON_PROLOGUE_NEVER_RAN


async def test_hard_cancel_mid_prologue_stops_before_any_later_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 cancel contract: a hard cancel tripping the turn's cancel token between
    prologue steps must stop it immediately -- no ``turn.started``, no memory
    event, no context frame recorded, no ``UserPromptSubmit`` hook subprocess
    started -- and the settle must still close the turn minter."""

    from clio_agent.gact.hooks import USER_PROMPT_SUBMIT, install_global_dispatcher
    from clio_agent.gact.part_atom_minter import turn_minter
    from clio_agent.gact.turn_prologue_guard import (
        REASON_CANCELLED_DURING_PROLOGUE,
        run_finalize_or_settle_prologue_gap,
    )
    from clio_agent.gact.turn_state import TurnCancelledDuringPrologue

    from ._hook_fixtures import make_command_dispatcher

    marker = tmp_path / "hook_ran.txt"
    # If the (cancelled) turn ever reached the hook dispatch, this would prove it.
    dispatcher = make_command_dispatcher(
        tmp_path,
        event=USER_PROMPT_SUBMIT,
        body=f"open({str(marker)!r}, 'w', encoding='utf-8').write('ran')",
    )
    install_global_dispatcher(dispatcher)
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
        sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
        app.state.sessions.update(sid, status="running")
        state = _new_prologue_shaped_state(app, sid, "msg_user_cancel")

        import clio_agent.gact.turn_start_offloop as offloop_module

        real_open_turn_minter = offloop_module.open_turn_minter

        def _open_then_cancel(app_: Any, sid_: str, turn_id_: str) -> Any:
            minter = real_open_turn_minter(app_, sid_, turn_id_)
            # Simulate the hard /cancel landing right after the minter opens --
            # THIS turn's own threading.Event, exactly as cancel_session_state trips it.
            state.turn_cancel_event.set()
            return minter

        emitted: list[str] = []
        published: list[str] = []
        frame_calls: list[Any] = []
        monkeypatch.setattr(offloop_module, "open_turn_minter", _open_then_cancel)
        monkeypatch.setattr(
            offloop_module,
            "_emit_semantic_event",
            lambda app_, sid_, event_type, **kw: emitted.append(event_type),
        )
        monkeypatch.setattr(
            offloop_module,
            "_publish_transcript_event",
            lambda bus_, sid_, event_type, payload_: published.append(event_type),
        )
        monkeypatch.setattr(
            offloop_module,
            "_record_context_frame",
            lambda *a, **k: frame_calls.append(1) or {"id": "unused"},
        )

        assert turn_minter(app, sid) is None  # nothing opened yet

        with pytest.raises(TurnCancelledDuringPrologue):
            offloop_module.prepare_turn_off_loop(state, update_retry_attempt=lambda *a, **k: None)

        assert state.prologue_phase == "running"
        assert turn_minter(app, sid) is not None  # step 1 (open_turn_minter) DID run
        assert "turn.started" not in emitted
        assert "turn.started" not in published
        assert "memory.search.completed" not in emitted
        assert frame_calls == []
        assert state.context_frame is None
        assert not marker.exists(), "the UserPromptSubmit hook must never have started"

        await run_finalize_or_settle_prologue_gap(
            state,
            drain_observed_tool_calls=lambda calls: calls,
            update_retry_attempt=lambda *args, **kwargs: None,
        )

        assert turn_minter(app, sid) is None, "every settle branch must close the turn minter"
        assert not marker.exists()

        history = app.state.bus._history.get(sid, [])
        completed = [ev for ev in history if ev.type == "message.completed"]
        assert completed
        error_info = completed[-1].payload["error_info"]
        assert error_info["error"] == REASON_CANCELLED_DURING_PROLOGUE
        assert error_info["details"]["reason"] == REASON_CANCELLED_DURING_PROLOGUE
        assert app.state.sessions.get(sid).status == "error"
    finally:
        install_global_dispatcher(None)


def test_finalize_turn_crashes_on_an_unset_context_frame_without_the_guard(
    tmp_path: Path,
) -> None:
    """Companion to the guard test above, proving the guard is load-bearing.

    ``finalize_turn`` itself is UNCHANGED -- it still assumes every prologue-
    derived field is set (that is exactly the contract ``prologue_phase``
    now enforces at the caller) -- so calling it directly, as ``turn.py`` used
    to unconditionally, on a state whose prologue never ran still crashes at
    the exact site the coordinator's CI trace named after #1338's ``b60a6a6f``
    fixed the sibling ``context_file_provenance`` field one level too
    narrowly: ``state.context_frame["id"]``.
    """

    from clio_agent.gact.turn_finalize import finalize_turn

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    sid = app.state.sessions.create(workspace_id="ws_default", title="t").id
    state = _new_prologue_shaped_state(app, sid, "msg_user_2")
    state.answer_text = "a real answer, so this isn't the empty_response branch"

    assert state.context_frame is None  # prologue never ran, never stubbed

    with pytest.raises(TypeError, match=r"NoneType.*subscriptable"):
        finalize_turn(
            state,
            object(),
            drain_observed_tool_calls=lambda calls: calls,
            update_retry_attempt=lambda *args, **kwargs: None,
        )
