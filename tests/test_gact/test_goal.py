"""P4.2 (#1080): run-until-a-condition GOAL, LLM-judge-only evaluation (A4 #1057).

Asserts the goal completion gate that rides the bounded Stop-loop re-drive seam:

* the PURE decision (:func:`evaluate_goal`) — the bounded LLM judge decides met / not-met,
  and typed bounds (max_goal_iters / budget) are the HARD STOPS that backstop an infinite
  loop with a typed reason (the deterministic goal-predicate tier was DELETED — nobody ships
  predicates over model-authored state; it let the model mark its own homework);
* the finalize-boundary orchestrator (:func:`dispatch_goal_at_finalize`, judge MOCKED) —
  re-drives when the judge says not-met (loop-inbox seam), settles + auto-clears when met;
* the model can NEVER set/clear a goal — there is no ``set_goal``/``goal_clear`` tool, only
  the READ-ONLY ``goal_status`` (armed-state only — it never runs the judge); the ``/goal``
  command + the ``arm_goal`` seam are the only arming doors;
* a ``when_state`` / ``predicate`` command arg no longer arms a self-satisfiable gate;
* goal state lives on ``session.metadata`` (no fifth store);
* compose with the loop (#1079) — a judge-met goal stops the loop with the typed
  ``loop_goal_met`` reason (the finalize glue, LLM-only).

Each body runs in a fresh ``contextvars.copy_context()`` so the (reset-less)
``set_app``/``set_session_id`` bindings never leak between tests.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from clio_agent.gact import context as _ctx
from clio_agent.gact import goal as goal_mod
from clio_agent.gact.goal import (
    DEFAULT_MAX_GOAL_ITERS,
    GOAL_OUTCOME_REASONS,
    GoalDecision,
    GoalError,
    GoalJudgement,
    arm_goal,
    build_goal_status_tool,
    clear_goal,
    dispatch_goal_at_finalize,
    evaluate_goal,
    parse_goal_command,
    run_goal_command,
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
    """A minimal app carrying only what the goal (and loop-compose) modules touch."""

    state = SimpleNamespace(
        schedules=ScheduleStore(path=tmp_path / "schedules.json"),
        deferred_schedules=set(),
        sessions=SessionStore(path=None),
        bus=_Bus(),
        messages={},  # dict-like ledger set: .get(sid, []) returns []
        loop_inboxes={},
        semantic_event_sink=None,  # _emit_semantic_event no-ops
    )
    return SimpleNamespace(state=state)


def _session(app: SimpleNamespace) -> str:
    return app.state.sessions.create(workspace_id="ws_default", title="goal").id


def _in_ctx(fn: Callable[[], None]) -> None:
    contextvars.copy_context().run(fn)


def _bind(app: object, sid: str) -> None:
    _ctx.set_app(app)
    _ctx.set_session_id(sid)


def _goal_state(app: SimpleNamespace, sid: str) -> dict[str, Any]:
    return dict(app.state.sessions.get(sid).metadata.get("goal") or {})


def _mock_judge(monkeypatch: Any, *, met: bool, reason: str = "") -> None:
    monkeypatch.setattr(
        goal_mod, "run_llm_judge", lambda app, sid, goal: GoalJudgement(met=met, reason=reason)
    )


# =========================================================================== #
# PURE decision (evaluate_goal) — the bounded LLM judge decides                 #
# =========================================================================== #
def test_evaluate_goal_settles_when_judge_met() -> None:
    goal = {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 10}
    decision = evaluate_goal(goal, llm=GoalJudgement(met=True, reason="looks done"))
    assert decision.outcome == "met"
    assert decision.reason == "goal_met"
    # auto-clear on met.
    assert decision.new_state["active"] is False
    assert decision.new_state["cleared"] is True
    assert decision.new_state["met"] is True


def test_evaluate_goal_redrives_when_judge_not_met() -> None:
    goal = {"condition": "the user seems satisfied", "iters_elapsed": 0, "max_goal_iters": 10}
    decision = evaluate_goal(goal, llm=GoalJudgement(met=False, reason="not yet"))
    assert decision.outcome == "redrive"
    assert decision.reason == "goal_redrive"
    assert decision.new_state["active"] is True
    assert decision.new_state["iters_elapsed"] == 1


def test_evaluate_goal_max_iters_backstop_typed_reason() -> None:
    """At the iteration ceiling with an unmet goal -> settle DONE, never re-drive forever.

    The loop bounds are the HARD STOPS (the deterministic predicate tier is gone)."""

    goal = {"condition": "c", "iters_elapsed": 5, "max_goal_iters": 5}
    decision = evaluate_goal(goal, llm=GoalJudgement(met=False, reason="still going"))
    assert decision.outcome == "capped"
    assert decision.reason == "goal_max_iters"
    assert decision.new_state["active"] is False
    assert decision.new_state["cleared"] is True


def test_evaluate_goal_budget_backstop_typed_reason() -> None:
    goal = {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 100, "max_wallclock_s": 10}
    decision = evaluate_goal(goal, llm=GoalJudgement(met=False, reason="working"), elapsed_s=30.0)
    assert decision.outcome == "capped"
    assert decision.reason == "goal_budget"
    # token budget also trips goal_budget.
    goal_t = {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 100, "max_tokens": 50}
    decision_t = evaluate_goal(goal_t, llm=GoalJudgement(met=False, reason="w"), tokens_spent=80)
    assert decision_t.outcome == "capped" and decision_t.reason == "goal_budget"


def test_evaluate_goal_unset_iters_uses_finite_default() -> None:
    """A goal with no explicit iteration bound still terminates (finite default ceiling)."""

    goal = {"condition": "c", "iters_elapsed": DEFAULT_MAX_GOAL_ITERS}
    decision = evaluate_goal(goal, llm=GoalJudgement(met=False, reason="w"))
    assert decision.outcome == "capped"
    assert decision.reason == "goal_max_iters"


# =========================================================================== #
# Orchestrator (dispatch_goal_at_finalize) — judge MOCKED                       #
# =========================================================================== #
def test_dispatch_redrives_when_judge_not_met(tmp_path: Path, monkeypatch: Any) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=False, reason="tests still failing")
        arm_goal(app, sid, condition="all tests pass")
        # judge says NOT met -> the finalize eval must re-drive one more turn.
        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome == "redrive"
        goal = _goal_state(app, sid)
        assert goal["active"] is True
        assert goal["iters_elapsed"] == 1
        # A re-drive was enqueued on the loop-inbox seam (the same one the Stop-loop rides).
        from clio_agent.gact.loop_inbox import inbox_for  # noqa: PLC0415

        events = inbox_for(app, sid).drain()
        assert len(events) == 1
        assert events[0].metadata.get("goal_redrive") is True

    _in_ctx(body)


def test_dispatch_settles_and_autoclears_when_judge_met(tmp_path: Path, monkeypatch: Any) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=True, reason="the summary is complete")
        arm_goal(app, sid, condition="write a good summary")
        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome == "met"
        goal = _goal_state(app, sid)
        assert goal["active"] is False  # auto-cleared
        assert goal["cleared"] is True
        assert goal["met"] is True
        # No re-drive enqueued on a met goal.
        from clio_agent.gact.loop_inbox import inbox_for  # noqa: PLC0415

        assert inbox_for(app, sid).drain() == []

    _in_ctx(body)


def test_dispatch_max_iters_settles_with_typed_reason(tmp_path: Path, monkeypatch: Any) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=False, reason="not yet")
        arm_goal(app, sid, condition="c", max_goal_iters=1)
        # Iteration 1: unmet -> re-drive (iters 0 < 1).
        d1 = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert d1 is not None and d1.outcome == "redrive"
        assert _goal_state(app, sid)["iters_elapsed"] == 1
        # Iteration 2: iters_elapsed (1) >= max (1) -> bounded stop, no infinite loop.
        d2 = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t2")
        assert d2 is not None and d2.outcome == "capped"
        assert d2.reason == "goal_max_iters"
        assert _goal_state(app, sid)["active"] is False

    _in_ctx(body)


def test_dispatch_noop_without_goal(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        assert dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1") is None
        assert app.state.sessions.get(sid).metadata.get("goal") is None

    _in_ctx(body)


def test_when_state_arg_does_not_create_self_satisfiable_gate(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A4 core: the deterministic goal tier is DELETED. A ``when_state`` / ``predicate``
    command arg no longer arms a gate the model can self-satisfy by writing ``workflow_state``.
    Completion is the bounded LLM judge only — a not-met judge re-drives even when the named
    state field is 'satisfied' (the self-grading anti-pattern is closed)."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=False, reason="not actually done")
        run_goal_command(
            app,
            sid,
            {
                "input": "the job is done",
                "args": {"when_state": {"field_path": "done", "check": "equals", "equals": "true"}},
            },
        )
        goal = _goal_state(app, sid)
        # No deterministic predicate is stored, and the goal is not predicate-backed.
        assert goal.get("predicate") is None
        assert goal.get("predicate_backed") in (None, False)
        # Even with the named state field 'satisfied', the not-met judge re-drives.
        app.state.sessions.update(sid, metadata_patch={"workflow_state": {"done": "true"}})
        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome == "redrive"

    _in_ctx(body)


# =========================================================================== #
# Injection-safety: NO model-callable set/clear tool; goal_status is armed-only #
# =========================================================================== #
def test_model_has_no_set_or_clear_goal_tool() -> None:
    from clio_agent.gact.agents.auto_tools import build_auto_react_tools

    agent_def = SimpleNamespace(id="agent", metadata={})
    names = {getattr(t, "name", "") for t in build_auto_react_tools(agent_def)}
    assert "goal_status" in names  # the read-only surface IS attached
    assert "set_goal" not in names  # ...but arming is NOT a model tool
    assert "goal_clear" not in names
    assert "goal_set" not in names


def test_goal_status_tool_builds_read_only() -> None:
    tool = build_goal_status_tool()
    assert tool.name == "goal_status"
    assert tool.args == {}  # a pure read-back, no inputs to mutate anything


def test_goal_status_does_not_run_judge(tmp_path: Path, monkeypatch: Any) -> None:
    """``goal_status`` returns ARMED STATE ONLY — it never runs the judge (or any gate) and
    never exposes a ``met`` / ``predicate_backed`` readback the model could steer toward."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)

        def _boom(*_a: Any, **_k: Any) -> GoalJudgement:
            raise AssertionError("goal_status must never run the judge")

        monkeypatch.setattr(goal_mod, "run_llm_judge", _boom)
        tool = build_goal_status_tool()
        # No goal yet.
        assert tool.func()["active"] is False
        arm_goal(app, sid, condition="ship the report", max_goal_iters=9)
        status = tool.func()  # must not raise (no judge call)
        assert status["active"] is True
        assert status["condition"] == "ship the report"
        assert status["max_goal_iters"] == 9
        assert "budget_spent" in status  # deterministic arithmetic stays
        # No completion readback the model could self-satisfy toward.
        assert "met" not in status
        assert "predicate_backed" not in status

    _in_ctx(body)


# =========================================================================== #
# /goal command + arm/clear seam                                               #
# =========================================================================== #
def test_parse_goal_command_condition_and_bounds() -> None:
    condition, bounds, clear = parse_goal_command(
        {"input": "all tests pass", "args": {"max_goal_iters": 8}}
    )
    assert clear is False
    assert condition == "all tests pass"
    assert bounds == {"max_goal_iters": 8}
    # a clear request.
    _c, _b, clear2 = parse_goal_command({"input": "clear"})
    assert clear2 is True


def test_goal_command_arms_and_clears(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        msg = run_goal_command(
            app, sid, {"input": "finish the analysis", "args": {"max_goal_iters": 5}}
        )
        assert "goal" in msg.lower()
        goal = _goal_state(app, sid)
        assert goal["active"] is True
        assert goal["condition"] == "finish the analysis"
        assert goal["max_goal_iters"] == 5
        # /goal clear removes it.
        cleared_msg = run_goal_command(app, sid, {"input": "clear"})
        assert "cleared" in cleared_msg.lower()
        assert _goal_state(app, sid)["active"] is False

    _in_ctx(body)


def test_goal_command_message_says_llm_judge(tmp_path: Path) -> None:
    """The /goal confirmation is honest about the LLM-judge-only contract + hard bounds."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        msg = run_goal_command(app, sid, {"input": "the report reads well"})
        assert "LLM judge" in msg
        assert "deterministic" not in msg.lower()

    _in_ctx(body)


def test_goal_command_usage_when_no_condition(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        msg = run_goal_command(app, sid, {"input": ""})
        assert "usage" in msg.lower()
        assert _goal_state(app, sid) == {}

    _in_ctx(body)


def test_arm_goal_rejects_empty_condition(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        raised = False
        try:
            arm_goal(app, sid, condition="   ")
        except GoalError as exc:
            raised = True
            assert exc.reason == "goal_missing_condition"
        assert raised

    _in_ctx(body)


def test_clear_goal_idempotent(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        assert clear_goal(app, sid) is False  # nothing to clear
        arm_goal(app, sid, condition="c")
        assert clear_goal(app, sid, reason="goal_abandoned") is True
        assert clear_goal(app, sid) is False  # already cleared

    _in_ctx(body)


# =========================================================================== #
# State on session.metadata (no fifth store)                                   #
# =========================================================================== #
def test_goal_state_lives_on_session_metadata(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        armed = arm_goal(app, sid, condition="triage", max_goal_iters=7)
        sess = app.state.sessions.get(sid)
        goal = sess.metadata["goal"]
        assert goal["goal_id"] == armed["goal_id"]
        assert goal["condition"] == "triage"
        assert goal["max_goal_iters"] == 7
        assert goal["active"] is True

    _in_ctx(body)


# =========================================================================== #
# Compose with the loop (#1079 loop_goal_met seam) — LLM-only, finalize glue    #
# =========================================================================== #
def test_loop_stops_with_loop_goal_met_when_judge_met_at_finalize(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """End-to-end compose (the turn_finalize glue): when the finalize goal eval settles ``met``
    (the bounded LLM judge), the armed loop is stopped with the typed ``loop_goal_met`` reason
    and its pending wakeup schedule is cancelled (cancel-both)."""

    def body() -> None:
        from clio_agent.gact.autonomous_loop import start_loop  # noqa: PLC0415
        from clio_agent.gact.turn_finalize import (  # noqa: PLC0415
            compose_goal_loop_stop_at_finalize,
        )

        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="keep working", interval_s=60, max_iters=100)
        pending = app.state.sessions.get(sid).metadata["loop"]["pending_schedule_id"]
        assert pending and app.state.schedules.get(pending) is not None
        _mock_judge(monkeypatch, met=True, reason="the deliverable is complete")
        arm_goal(app, sid, condition="the deliverable is complete")

        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome == "met"
        # Drive the SHIPPED finalize seam (the exact function turn_finalize.finalize_turn
        # calls) — not a hand-rolled stop — so deleting its stop_session_loop body turns
        # this test red (the A4 review: the glue must be verified, not silently deletable).
        stopped = compose_goal_loop_stop_at_finalize(app, sid, decision)
        assert stopped is True

        loop = app.state.sessions.get(sid).metadata["loop"]
        assert loop["stopped"] is True
        assert loop["stop_reason"] == "loop_goal_met"
        # The pending wakeup schedule was cancelled (no orphan re-fire).
        assert app.state.schedules.get(pending) is None
        assert app.state.schedules.list(session_id=sid) == []

    _in_ctx(body)


# =========================================================================== #
# The finalize seam is inert when the goal did not settle 'met'                 #
# =========================================================================== #
def test_finalize_seam_leaves_loop_running_when_goal_not_met(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The shipped seam is a no-op unless the judge settled ``met``: an unmet goal (or no
    goal at all) must NOT stop the armed loop. Guards the seam against firing on the wrong
    outcome — the counterpart to the met-path test above."""

    def body() -> None:
        from clio_agent.gact.autonomous_loop import start_loop  # noqa: PLC0415
        from clio_agent.gact.turn_finalize import (  # noqa: PLC0415
            compose_goal_loop_stop_at_finalize,
        )

        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="keep working", interval_s=60, max_iters=100)
        _mock_judge(monkeypatch, met=False, reason="not done yet")
        arm_goal(app, sid, condition="the deliverable is complete")

        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome != "met"
        assert compose_goal_loop_stop_at_finalize(app, sid, decision) is False
        # A None decision (no goal armed) is inert too.
        assert compose_goal_loop_stop_at_finalize(app, sid, None) is False

        loop = app.state.sessions.get(sid).metadata["loop"]
        assert loop.get("stopped") is not True

    _in_ctx(body)


# =========================================================================== #
# Typed catalogs + command/tool registration                                   #
# =========================================================================== #
def test_goal_outcome_reasons_are_typed() -> None:
    for reason in (
        "goal_met",
        "goal_max_iters",
        "goal_budget",
        "goal_abandoned",
        "goal_redrive",
    ):
        assert reason in GOAL_OUTCOME_REASONS
    # the deterministic-override reason was DELETED with the deterministic tier.
    assert "goal_llm_overridden" not in GOAL_OUTCOME_REASONS


def test_goal_command_row_exists() -> None:
    from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

    row = next((c for c in BACKEND_COMMANDS if c["id"] == "/goal"), None)
    assert row is not None
    assert row["status"] == "available"
    assert row["enabled"] is True


def test_evaluate_goal_returns_goal_decision_type() -> None:
    decision = evaluate_goal(
        {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 3},
        llm=GoalJudgement(met=True),
    )
    assert isinstance(decision, GoalDecision)
