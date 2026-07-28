"""P4.1 (#1079): the autonomous cross-turn LOOP — self-pace + typed bounds + fallback.

Asserts the loop primitive built on the P4.3 scheduler one-shot:

* ``loop_wakeup`` clamps its delay into [60, 3600] with a TYPED reason (no silent clamp);
* the loop stops on EACH typed bound — max_iters / max_wallclock / token-budget / stall
  (progress-liveness via ``workflow_step_watch``) — with a structured reason;
* the bounded fallback fires exactly once then ENDS when a turn neither reschedules nor
  stops (``dispatch_loop_at_finalize``);
* ``stop:true`` ends immediately;
* loop state lives on ``session.metadata`` (no fifth store);
* CANCEL-BOTH — ending/cancelling the session cancels the pending wakeup (no orphan
  schedule survives);
* the ``/loop`` command row exists and ``loop_wakeup`` is auto-attached.

Each body runs in a fresh ``contextvars.copy_context()`` so the (reset-less)
``set_app``/``set_session_id`` bindings never leak between tests.
"""

from __future__ import annotations

import contextvars
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from clio_agent.gact import autonomous_loop as loop_mod
from clio_agent.gact import context as _ctx
from clio_agent.gact.autonomous_loop import (
    CLAMP_CEILING,
    CLAMP_FLOOR,
    LoopError,
    build_loop_wakeup_tool,
    clamp_delay,
    dispatch_loop_at_finalize,
    loop_wakeup_impl,
    parse_loop_command,
    start_loop,
    stop_session_loop,
)
from clio_agent.gact.scheduler import ScheduleStore
from clio_agent.gact.sessions import SessionStore


class _Bus:
    """Minimal bus exposing the activity heartbeat the loop's stall probe reads."""

    def __init__(self) -> None:
        self._m: dict[str, float] = {}

    def last_publish_monotonic(self, sid: str) -> float:
        return self._m.get(sid, 0.0)

    def bump(self, sid: str, value: float) -> None:
        self._m[sid] = value


def _app(tmp_path: Path) -> SimpleNamespace:
    """A minimal app carrying only what the loop module touches."""

    state = SimpleNamespace(
        schedules=ScheduleStore(path=tmp_path / "schedules.json"),
        deferred_schedules=set(),
        sessions=SessionStore(path=None),
        bus=_Bus(),
    )
    return SimpleNamespace(state=state)


def _session(app: SimpleNamespace) -> str:
    return app.state.sessions.create(workspace_id="ws_default", title="loop").id


def _in_ctx(fn: Callable[[], None]) -> None:
    contextvars.copy_context().run(fn)


def _bind(app: object, sid: str) -> None:
    _ctx.set_app(app)
    _ctx.set_session_id(sid)


def _loop_state(app: SimpleNamespace, sid: str) -> dict[str, Any]:
    return dict(app.state.sessions.get(sid).metadata.get("loop") or {})


# --------------------------------------------------------------------------- #
# Clamp                                                                         #
# --------------------------------------------------------------------------- #
def test_clamp_delay_typed_reasons() -> None:
    assert clamp_delay(10) == (60, CLAMP_FLOOR)
    assert clamp_delay(999_999) == (3600, CLAMP_CEILING)
    assert clamp_delay(300) == (300, "")


def test_loop_wakeup_clamps_delay_with_typed_reason(tmp_path: Path) -> None:
    """A sub-floor delay is clamped and the typed clamp reason is recorded (no silent)."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="keep going", interval_s=300)
        # simulate the armed wakeup firing: the one-shot is popped by the scheduler.
        app.state.schedules.delete(_loop_state(app, sid)["pending_schedule_id"])
        app.state.bus.bump(sid, 5.0)  # progress so stall does not trip

        result = loop_wakeup_impl(delay_seconds=1, prompt="keep going", reason="tick")
        assert result["stopped"] is False
        loop = _loop_state(app, sid)
        assert loop["clamp_reason"] == CLAMP_FLOOR
        # the newly armed one-shot fires 60s (the floor) out, not 1s.
        sch = app.state.schedules.get(loop["pending_schedule_id"])
        assert sch is not None

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Typed bounds                                                                  #
# --------------------------------------------------------------------------- #
def test_loop_stops_on_max_iters(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=2)
        # Iteration 1: progresses, arms again.
        app.state.bus.bump(sid, 1.0)
        r1 = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert r1["stopped"] is False
        # Iteration 2: reaching max_iters -> stop with the typed reason.
        app.state.bus.bump(sid, 2.0)
        r2 = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert r2["stopped"] is True
        loop = _loop_state(app, sid)
        assert loop["stop_reason"] == "loop_max_iters"
        assert loop["active"] is False
        # cancel-both: no pending wakeup survives.
        assert app.state.schedules.list(session_id=sid) == []

    _in_ctx(body)


def test_loop_stops_on_max_wallclock(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=100, max_wallclock_s=5)
        # Backdate the loop's creation so the wall-clock budget is already exhausted.
        loop = _loop_state(app, sid)
        loop["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        app.state.sessions.update(sid, metadata_patch={"loop": loop})
        app.state.bus.bump(sid, 1.0)  # progress so stall does not trip first

        result = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert result["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_budget"

    _in_ctx(body)


def test_loop_stops_on_token_budget(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=100, max_tokens=100)
        app.state.sessions.update(sid, add_tokens_input=150, add_tokens_output=50)
        app.state.bus.bump(sid, 1.0)

        result = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert result["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_budget"

    _in_ctx(body)


def test_loop_stops_on_stall_via_progress_liveness(tmp_path: Path) -> None:
    """No bus activity across iterations -> stall stop (workflow_step_watch signal)."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        # Bus heartbeat stays 0.0 (never bumped) => no observable progress per iteration.
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=100, max_no_progress=2)
        r1 = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert r1["stopped"] is False
        r2 = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert r2["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_stalled"

    _in_ctx(body)


def test_progress_resets_stall_counter(tmp_path: Path) -> None:
    """An iteration that DOES publish resets the no-progress counter (not a false stall)."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=100, max_no_progress=2)
        app.state.bus.bump(sid, 1.0)
        loop_wakeup_impl(delay_seconds=60, prompt="p")  # progress
        app.state.bus.bump(sid, 2.0)
        loop_wakeup_impl(delay_seconds=60, prompt="p")  # progress
        app.state.bus.bump(sid, 3.0)
        r = loop_wakeup_impl(delay_seconds=60, prompt="p")  # progress
        assert r["stopped"] is False
        assert _loop_state(app, sid)["no_progress_count"] == 0

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# stop:true                                                                     #
# --------------------------------------------------------------------------- #
def test_stop_true_ends_immediately_and_cancels_wakeup(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=120)
        assert app.state.schedules.list(session_id=sid)  # a wakeup is armed

        result = loop_wakeup_impl(stop=True, reason="done")
        assert result["stopped"] is True
        loop = _loop_state(app, sid)
        assert loop["active"] is False
        assert loop["stop_reason"] == "loop_user_stopped"
        # cancel-both: the pending wakeup is gone.
        assert app.state.schedules.list(session_id=sid) == []

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Bounded fallback                                                              #
# --------------------------------------------------------------------------- #
def test_bounded_fallback_fires_once_then_ends(tmp_path: Path) -> None:
    """A loop turn that neither reschedules nor stops -> ONE fallback -> then ends."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60)
        first_id = _loop_state(app, sid)["pending_schedule_id"]

        # Simulate the armed wakeup firing (scheduler pops the one-shot), then a turn that
        # armed NO new wakeup -> the finalize hook arms exactly one fallback.
        app.state.schedules.delete(first_id)
        dispatch_loop_at_finalize(app, session_id=sid, turn_id="t1")
        loop = _loop_state(app, sid)
        assert loop["fallback_pending"] is True
        assert loop["active"] is True
        fallback_id = loop["pending_schedule_id"]
        assert fallback_id and fallback_id != first_id
        assert app.state.schedules.get(fallback_id) is not None

        # The fallback fires and STILL no reschedule -> the loop ends (typed reason).
        app.state.schedules.delete(fallback_id)
        dispatch_loop_at_finalize(app, session_id=sid, turn_id="t2")
        loop = _loop_state(app, sid)
        assert loop["active"] is False
        assert loop["stop_reason"] == "loop_no_reschedule"
        assert app.state.schedules.list(session_id=sid) == []

    _in_ctx(body)


def test_finalize_hook_noop_when_model_rescheduled(tmp_path: Path) -> None:
    """When the model armed a fresh wakeup this turn, the fallback hook does nothing."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60)
        first_id = _loop_state(app, sid)["pending_schedule_id"]
        # Wakeup fired (popped), then the model rescheduled via loop_wakeup.
        app.state.schedules.delete(first_id)
        app.state.bus.bump(sid, 1.0)
        loop_wakeup_impl(delay_seconds=60, prompt="p")
        new_id = _loop_state(app, sid)["pending_schedule_id"]
        assert new_id and new_id != first_id

        dispatch_loop_at_finalize(app, session_id=sid, turn_id="t1")
        loop = _loop_state(app, sid)
        assert loop["active"] is True
        assert loop["fallback_pending"] is False
        assert loop["pending_schedule_id"] == new_id

    _in_ctx(body)


def test_finalize_hook_noop_when_no_loop(tmp_path: Path) -> None:
    """The finalize hook is a safe no-op for a session with no active loop."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        dispatch_loop_at_finalize(app, session_id=sid, turn_id="t1")
        assert app.state.sessions.get(sid).metadata.get("loop") is None

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# State on session.metadata                                                     #
# --------------------------------------------------------------------------- #
def test_loop_state_lives_on_session_metadata(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        started = start_loop(app, sid, prompt="triage", interval_s=120, max_iters=7)
        sess = app.state.sessions.get(sid)
        loop = sess.metadata["loop"]
        assert loop["loop_id"] == started["loop_id"]
        assert loop["prompt"] == "triage"
        assert loop["max_iters"] == 7
        assert loop["interval_s"] == 120
        assert loop["active"] is True
        assert loop["pending_schedule_id"]

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Cancel-both on restart (orphaned wakeup regression — adversarial finding 1)   #
# --------------------------------------------------------------------------- #
def test_start_loop_cancels_prior_pending_wakeup(tmp_path: Path) -> None:
    """Restarting a loop (a second start_loop on the same session) must cancel the
    PRIOR loop's pending wakeup, not just overwrite metadata — else the old one-shot
    survives in the schedule store and fires unattended later (orphaned wakeup),
    violating the module's "at most one loop wakeup is ever armed" invariant."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="first prompt", interval_s=120)
        sched_a = _loop_state(app, sid)["pending_schedule_id"]
        assert app.state.schedules.get(sched_a) is not None

        start_loop(app, sid, prompt="second prompt", interval_s=120)
        sched_b = _loop_state(app, sid)["pending_schedule_id"]
        assert sched_b != sched_a

        # sched_A must be cancelled — only sched_B remains armed. No orphan.
        remaining = app.state.schedules.list(session_id=sid)
        remaining_ids = [s.id for s in remaining]
        assert sched_a not in remaining_ids
        assert remaining_ids == [sched_b]

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Budget bound measures loop delta, not cumulative session (adversarial finding 2) #
# --------------------------------------------------------------------------- #
def test_loop_budget_measures_delta_since_loop_start(tmp_path: Path) -> None:
    """max_tokens must bound tokens the LOOP ITSELF spends, not the session's
    cumulative rollup. A session that already spent > max_tokens before the loop
    started must NOT trip loop_budget on iteration 1 — only once the loop's own
    delta reaches max_tokens."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        # Session already has pre-existing usage exceeding the loop's max_tokens.
        app.state.sessions.update(sid, add_tokens_input=500, add_tokens_output=100)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=100, max_tokens=100)
        app.state.bus.bump(sid, 1.0)

        # Iteration 1: cumulative (600) >= max_tokens (100), but the loop itself has
        # spent nothing yet -> must NOT trip loop_budget.
        r1 = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert r1["stopped"] is False
        assert _loop_state(app, sid)["stop_reason"] == ""

        # Now the loop itself spends 150 tokens (delta) -> trips loop_budget.
        app.state.sessions.update(sid, add_tokens_input=150)
        app.state.bus.bump(sid, 2.0)
        r2 = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert r2["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_budget"

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Cancel-both on session end                                                    #
# --------------------------------------------------------------------------- #
def test_stop_session_loop_cancels_pending_wakeup(tmp_path: Path) -> None:
    """Ending the session cancels the pending loop wakeup — no orphan schedule."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=120)
        assert app.state.schedules.list(session_id=sid)

        stop_session_loop(app, sid)
        loop = _loop_state(app, sid)
        assert loop["active"] is False
        assert loop["stop_reason"] == "loop_session_ended"
        assert app.state.schedules.list(session_id=sid) == []  # no orphan wakeup

    _in_ctx(body)


def test_stop_session_loop_noop_without_loop(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        stop_session_loop(app, sid)  # must not raise
        assert app.state.sessions.get(sid).metadata.get("loop") is None

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Self-initiated loop via the tool                                             #
# --------------------------------------------------------------------------- #
def test_loop_wakeup_self_initiates_when_no_loop(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        result = loop_wakeup_impl(delay_seconds=120, prompt="start looping")
        assert result["stopped"] is False
        assert result["loop_id"].startswith("loop_")
        loop = _loop_state(app, sid)
        assert loop["active"] is True
        assert loop["prompt"] == "start looping"

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# Sticky bounds — a tripped hard bound cannot be re-armed by the model (A1)      #
# --------------------------------------------------------------------------- #
def test_rearm_denied_after_max_iters(tmp_path: Path) -> None:
    """A loop that hit ``loop_max_iters`` cannot be re-armed by a model ``loop_wakeup``;
    the user must re-issue ``/loop``. Also pins GRACE — the bound trip RETURNS
    ``{stopped: True}`` (the turn continues), it does not raise."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=1)
        app.state.bus.bump(sid, 1.0)  # progress so stall does not trip first

        # Grace: reaching max_iters RETURNS stopped (turn continues), never raises.
        tripped = loop_wakeup_impl(delay_seconds=60, prompt="p")
        assert tripped["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_max_iters"

        # A model re-arm attempt on the tripped loop is DENIED (sticky bound).
        raised = False
        try:
            loop_wakeup_impl(delay_seconds=60, prompt="again")
        except LoopError as exc:
            raised = True
            assert exc.reason == "loop_bound_tripped_rearm_denied"
        assert raised
        # No new wakeup was armed by the denied re-arm.
        assert app.state.schedules.list(session_id=sid) == []

    _in_ctx(body)


def test_rearm_denied_after_session_cancel(tmp_path: Path) -> None:
    """A loop cancelled by session end (``loop_session_ended``) is sticky too — a model
    ``loop_wakeup`` cannot resurrect it."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=120)
        stop_session_loop(app, sid)
        assert _loop_state(app, sid)["stop_reason"] == "loop_session_ended"

        raised = False
        try:
            loop_wakeup_impl(delay_seconds=60, prompt="again")
        except LoopError as exc:
            raised = True
            assert exc.reason == "loop_bound_tripped_rearm_denied"
        assert raised
        assert app.state.schedules.list(session_id=sid) == []

    _in_ctx(body)


def test_rearm_allowed_after_model_stop(tmp_path: Path) -> None:
    """The model's OWN ``stop=True`` (``loop_user_stopped``) is NOT a tripped bound, so it
    is not sticky — a subsequent non-stop ``loop_wakeup`` self-initiates a fresh loop."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=120)
        stopped = loop_wakeup_impl(stop=True, reason="done")
        assert stopped["stopped"] is True
        assert _loop_state(app, sid)["stop_reason"] == "loop_user_stopped"

        restarted = loop_wakeup_impl(delay_seconds=120, prompt="resume")
        assert restarted["stopped"] is False
        assert restarted["loop_id"].startswith("loop_")
        loop = _loop_state(app, sid)
        assert loop["active"] is True
        assert loop["prompt"] == "resume"

    _in_ctx(body)


def test_user_loop_restart_clears_sticky(tmp_path: Path) -> None:
    """The USER path (``start_loop`` / ``/loop``) writes a fresh loop dict, clearing a
    prior sticky stop so a model ``loop_wakeup`` works again."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="p", interval_s=60, max_iters=1)
        app.state.bus.bump(sid, 1.0)
        loop_wakeup_impl(delay_seconds=60, prompt="p")  # trips sticky loop_max_iters
        assert _loop_state(app, sid)["stop_reason"] == "loop_max_iters"

        # The user re-issues /loop — a fresh dict clears the sticky state.
        start_loop(app, sid, prompt="restart", interval_s=60, max_iters=5)
        loop = _loop_state(app, sid)
        assert loop["active"] is True
        assert loop["stopped"] is False
        assert loop["stop_reason"] == ""
        assert loop["prompt"] == "restart"

        # A model wakeup now succeeds again (not denied).
        app.state.bus.bump(sid, 2.0)
        resumed = loop_wakeup_impl(delay_seconds=60, prompt="restart")
        assert resumed["stopped"] is False

    _in_ctx(body)


# --------------------------------------------------------------------------- #
# /loop command + tool registration                                            #
# --------------------------------------------------------------------------- #
def test_loop_command_row_exists() -> None:
    from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

    row = next((c for c in BACKEND_COMMANDS if c["id"] == "/loop"), None)
    assert row is not None
    assert row["status"] == "available"
    assert row["enabled"] is True


def test_parse_loop_command_interval_and_bounds() -> None:
    interval_s, prompt, bounds = parse_loop_command(
        {"input": "5m keep triaging PRs", "args": {"max_iters": 10}}
    )
    assert interval_s == 300
    assert prompt == "keep triaging PRs"
    assert bounds == {"max_iters": 10}

    # A bare prompt (no leading interval token) is not swallowed as an interval.
    interval_s2, prompt2, _ = parse_loop_command({"input": "keep working"})
    assert interval_s2 == 0
    assert prompt2 == "keep working"


def test_loop_command_arms_a_loop(tmp_path: Path) -> None:
    """The /loop parse+start path sets state on metadata and arms a wakeup."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        interval_s, prompt, bounds = parse_loop_command(
            {"input": "2m keep going", "args": {"max_iters": 3}}
        )
        started = start_loop(app, sid, prompt=prompt, interval_s=interval_s, **bounds)
        assert started["interval_s"] == 120
        assert started["max_iters"] == 3
        loop = _loop_state(app, sid)
        assert loop["active"] is True
        assert app.state.schedules.get(loop["pending_schedule_id"]) is not None

    _in_ctx(body)


def test_loop_wakeup_tool_is_auto_attached() -> None:
    from clio_agent.gact.agents.auto_tools import build_auto_react_tools

    agent_def = SimpleNamespace(id="agent", metadata={})
    tools = build_auto_react_tools(agent_def)
    assert any(getattr(t, "name", "") == "loop_wakeup" for t in tools)


def test_loop_wakeup_tool_builds_with_schema() -> None:
    tool = build_loop_wakeup_tool()
    assert tool.name == "loop_wakeup"
    assert set(tool.args) == {"delay_seconds", "prompt", "reason", "stop"}


def test_start_loop_rejects_empty_prompt(tmp_path: Path) -> None:
    from clio_agent.gact.autonomous_loop import LoopError

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        raised = False
        try:
            start_loop(app, sid, prompt="   ", interval_s=60)
        except LoopError as exc:
            raised = True
            assert exc.reason == "loop_missing_prompt"
        assert raised

    _in_ctx(body)


def test_module_stop_reasons_are_typed() -> None:
    # Every reason the module emits is in the declared typed catalog.
    for reason in (
        "loop_max_iters",
        "loop_budget",
        "loop_stalled",
        "loop_user_stopped",
        "loop_goal_met",
        "loop_no_reschedule",
        "loop_session_ended",
    ):
        assert reason in loop_mod.LOOP_STOP_REASONS
