"""P4.2 (#1080): run-until-a-predicate GOAL conditions with two-tier evaluation.

Asserts the goal completion gate that rides the bounded Stop-loop re-drive seam:

* the PURE two-tier decision (:func:`evaluate_goal`) — the deterministic tier is
  AUTHORITATIVE (a false LLM "met" is overridden), the NL-only mode falls back to the
  LLM tier (flagged), and typed bounds (max_goal_iters / budget) backstop against an
  infinite loop with a typed reason;
* the finalize-boundary orchestrator (:func:`dispatch_goal_at_finalize`, judge MOCKED) —
  re-drives when unmet (loop-inbox seam), settles + auto-clears when met, and a prose
  "goal met" NEVER satisfies a predicate-backed goal (injection-safe);
* the model can NEVER set/clear a goal — there is no ``set_goal``/``goal_clear`` tool, only
  the READ-ONLY ``goal_status``; the ``/goal`` command + the ``arm_goal`` seam are the only
  arming doors;
* goal state lives on ``session.metadata`` (no fifth store);
* compose with the loop (#1079) — a satisfied predicate-backed goal ends the loop with the
  typed ``loop_goal_met`` reason.

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
    combine_tiers,
    dispatch_goal_at_finalize,
    evaluate_goal,
    loop_goal_satisfied,
    parse_goal_command,
    run_deterministic_gate,
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


def _set_ws(app: SimpleNamespace, sid: str, ws: dict[str, Any]) -> None:
    app.state.sessions.update(sid, metadata_patch={"workflow_state": ws})


# =========================================================================== #
# PURE two-tier decision (evaluate_goal / combine_tiers)                        #
# =========================================================================== #
def test_combine_tiers_deterministic_is_authoritative() -> None:
    # Deterministic True wins even when the LLM says not met.
    met, tier, _reason, overridden = combine_tiers(
        det_result=True, llm=GoalJudgement(met=False, reason="looks unfinished")
    )
    assert met is True and tier == "deterministic" and overridden is False
    # Deterministic False OVERRIDES a false LLM "met" (flagged).
    met2, tier2, _r2, overridden2 = combine_tiers(
        det_result=False, llm=GoalJudgement(met=True, reason="tests pass")
    )
    assert met2 is False and tier2 == "deterministic" and overridden2 is True
    # NL-only: the LLM tier decides (weaker mode, flagged tier == "llm").
    met3, tier3, _r3, overridden3 = combine_tiers(
        det_result=None, llm=GoalJudgement(met=True, reason="done")
    )
    assert met3 is True and tier3 == "llm" and overridden3 is False


def test_evaluate_goal_settles_when_deterministic_met() -> None:
    goal = {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 10}
    decision = evaluate_goal(goal, det_result=True, llm=GoalJudgement(met=False, reason="unsure"))
    assert decision.outcome == "met"
    assert decision.reason == "goal_met"
    assert decision.tier == "deterministic"
    # auto-clear on met.
    assert decision.new_state["active"] is False
    assert decision.new_state["cleared"] is True
    assert decision.new_state["met"] is True


def test_evaluate_goal_deterministic_overrides_false_llm_met() -> None:
    """The LLM proposes met, the deterministic gate says NO -> re-drive (overridden)."""

    goal = {"condition": "all tests pass", "iters_elapsed": 0, "max_goal_iters": 10}
    decision = evaluate_goal(
        goal, det_result=False, llm=GoalJudgement(met=True, reason="I ran the tests")
    )
    assert decision.outcome == "redrive"
    assert decision.reason == "goal_llm_overridden"
    assert decision.llm_overridden is True
    assert decision.met is False
    assert decision.new_state["active"] is True
    assert decision.new_state["iters_elapsed"] == 1


def test_evaluate_goal_nl_only_uses_llm_tier_flagged() -> None:
    goal = {"condition": "the user seems satisfied", "iters_elapsed": 0, "max_goal_iters": 10}
    decision = evaluate_goal(
        goal, det_result=None, llm=GoalJudgement(met=True, reason="user said thanks")
    )
    assert decision.outcome == "met"
    assert decision.tier == "llm"  # the weaker NL-only mode is flagged
    # And an NL-only unmet re-drives on the LLM tier.
    decision2 = evaluate_goal(
        {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 10},
        det_result=None,
        llm=GoalJudgement(met=False, reason="not yet"),
    )
    assert decision2.outcome == "redrive"
    assert decision2.reason == "goal_redrive"
    assert decision2.tier == "llm"


def test_evaluate_goal_max_iters_backstop_typed_reason() -> None:
    """At the iteration ceiling with an unmet goal -> settle DONE, never re-drive forever."""

    goal = {"condition": "c", "iters_elapsed": 5, "max_goal_iters": 5}
    decision = evaluate_goal(
        goal, det_result=False, llm=GoalJudgement(met=False, reason="still going")
    )
    assert decision.outcome == "capped"
    assert decision.reason == "goal_max_iters"
    assert decision.new_state["active"] is False
    assert decision.new_state["cleared"] is True


def test_evaluate_goal_budget_backstop_typed_reason() -> None:
    goal = {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 100, "max_wallclock_s": 10}
    decision = evaluate_goal(
        goal,
        det_result=False,
        llm=GoalJudgement(met=False, reason="working"),
        elapsed_s=30.0,
    )
    assert decision.outcome == "capped"
    assert decision.reason == "goal_budget"
    # token budget also trips goal_budget.
    goal_t = {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 100, "max_tokens": 50}
    decision_t = evaluate_goal(
        goal_t, det_result=False, llm=GoalJudgement(met=False, reason="w"), tokens_spent=80
    )
    assert decision_t.outcome == "capped" and decision_t.reason == "goal_budget"


def test_evaluate_goal_unset_iters_uses_finite_default() -> None:
    """A goal with no explicit iteration bound still terminates (finite default ceiling)."""

    goal = {"condition": "c", "iters_elapsed": DEFAULT_MAX_GOAL_ITERS}
    decision = evaluate_goal(goal, det_result=False, llm=GoalJudgement(met=False, reason="w"))
    assert decision.outcome == "capped"
    assert decision.reason == "goal_max_iters"


# =========================================================================== #
# Deterministic gate (StatePredicate over workflow_state) — reuse #948 vocab   #
# =========================================================================== #
def test_run_deterministic_gate_state_exists_and_equals(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        # exists gate over the session's typed workflow_state.
        arm_goal(
            app,
            sid,
            condition="acquisition done",
            predicate={
                "kind": "state",
                "field_path": "acquisition.status",
                "check": "exists",
                "exists": True,
            },
        )
        goal = _goal_state(app, sid)
        assert run_deterministic_gate(app, sid, goal) is False  # not present yet
        _set_ws(app, sid, {"acquisition": {"status": "done"}})
        assert run_deterministic_gate(app, sid, goal) is True

        # equals gate.
        arm_goal(
            app,
            sid,
            condition="status == done",
            predicate={
                "kind": "state",
                "field_path": "acquisition.status",
                "check": "equals",
                "equals": "done",
            },
        )
        goal2 = _goal_state(app, sid)
        assert run_deterministic_gate(app, sid, goal2) is True
        _set_ws(app, sid, {"acquisition": {"status": "pending"}})
        assert run_deterministic_gate(app, sid, goal2) is False

    _in_ctx(body)


def test_run_deterministic_gate_nl_only_is_none(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        arm_goal(app, sid, condition="the report reads well")  # no predicate
        assert run_deterministic_gate(app, sid, _goal_state(app, sid)) is None

    _in_ctx(body)


def test_run_deterministic_gate_file_exists(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        target = tmp_path / "artifact.txt"
        arm_goal(
            app,
            sid,
            condition="artifact written",
            predicate={"kind": "file_exists", "path": str(target)},
        )
        goal = _goal_state(app, sid)
        assert run_deterministic_gate(app, sid, goal) is False
        target.write_text("done", encoding="utf-8")
        assert run_deterministic_gate(app, sid, goal) is True

    _in_ctx(body)


# =========================================================================== #
# Orchestrator (dispatch_goal_at_finalize) — judge MOCKED                       #
# =========================================================================== #
def _mock_judge(monkeypatch: Any, *, met: bool, reason: str = "") -> None:
    monkeypatch.setattr(
        goal_mod, "run_llm_judge", lambda app, sid, goal: GoalJudgement(met=met, reason=reason)
    )


def test_dispatch_redrives_when_predicate_unmet(tmp_path: Path, monkeypatch: Any) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=False, reason="tests still failing")
        arm_goal(
            app,
            sid,
            condition="all tests pass",
            predicate={
                "kind": "state",
                "field_path": "tests.passing",
                "check": "equals",
                "equals": True,
            },
        )
        # predicate NOT satisfied -> the finalize eval must re-drive one more turn.
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


def test_dispatch_settles_and_autoclears_when_deterministic_met(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        # The judge says NOT met, but the deterministic gate holds -> deterministic wins.
        _mock_judge(monkeypatch, met=False, reason="I am unsure")
        arm_goal(
            app,
            sid,
            condition="tests pass",
            predicate={
                "kind": "state",
                "field_path": "tests.passing",
                "check": "equals",
                "equals": True,
            },
        )
        _set_ws(app, sid, {"tests": {"passing": True}})
        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome == "met"
        assert decision.tier == "deterministic"
        goal = _goal_state(app, sid)
        assert goal["active"] is False  # auto-cleared
        assert goal["cleared"] is True
        assert goal["met"] is True
        # No re-drive enqueued on a met goal.
        from clio_agent.gact.loop_inbox import inbox_for  # noqa: PLC0415

        assert inbox_for(app, sid).drain() == []

    _in_ctx(body)


def test_dispatch_prose_cannot_satisfy_predicate_backed_goal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Injection-safety: a transcript claim "tests pass" (LLM met=True) must NOT satisfy a
    predicate-backed goal whose deterministic gate does not confirm — it re-drives instead."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=True, reason="the transcript says all tests pass")
        arm_goal(
            app,
            sid,
            condition="all tests pass",
            predicate={
                "kind": "state",
                "field_path": "tests.passing",
                "check": "equals",
                "equals": True,
            },
        )
        # workflow_state does NOT show tests.passing == True (the real gate is unmet).
        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None
        assert decision.outcome == "redrive"  # NOT met despite the prose claim
        assert decision.reason == "goal_llm_overridden"
        assert decision.llm_overridden is True
        assert _goal_state(app, sid)["active"] is True  # still gating, not satisfied

    _in_ctx(body)


def test_dispatch_max_iters_settles_with_typed_reason(tmp_path: Path, monkeypatch: Any) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=False, reason="not yet")
        arm_goal(
            app,
            sid,
            condition="c",
            predicate={"kind": "state", "field_path": "x", "check": "exists", "exists": True},
            max_goal_iters=1,
        )
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


def test_dispatch_nl_only_uses_llm_judge(tmp_path: Path, monkeypatch: Any) -> None:
    """An NL-only goal (no predicate) settles on the LLM tier (the flagged weaker mode)."""

    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        _mock_judge(monkeypatch, met=True, reason="the summary is complete")
        arm_goal(app, sid, condition="write a good summary")
        decision = dispatch_goal_at_finalize(app, session_id=sid, turn_id="t1")
        assert decision is not None and decision.outcome == "met"
        assert decision.tier == "llm"
        assert _goal_state(app, sid)["active"] is False

    _in_ctx(body)


# =========================================================================== #
# Injection-safety: NO model-callable set/clear tool                           #
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


def test_goal_status_reflects_state_deterministic_met(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        tool = build_goal_status_tool()
        # No goal yet.
        assert tool.func()["active"] is False
        arm_goal(
            app,
            sid,
            condition="done flag set",
            predicate={"kind": "state", "field_path": "done", "check": "exists", "exists": True},
            max_goal_iters=9,
        )
        status = tool.func()
        assert status["active"] is True
        assert status["condition"] == "done flag set"
        assert status["predicate_backed"] is True
        assert status["max_goal_iters"] == 9
        assert status["met"] is False  # deterministic gate not yet satisfied
        _set_ws(app, sid, {"done": True})
        assert tool.func()["met"] is True  # read-only readback reflects the real gate

    _in_ctx(body)


# =========================================================================== #
# /goal command + arm/clear seam                                               #
# =========================================================================== #
def test_parse_goal_command_condition_bounds_predicate() -> None:
    condition, bounds, predicate, clear = parse_goal_command(
        {
            "input": "all tests pass",
            "args": {
                "max_goal_iters": 8,
                "predicate": {
                    "kind": "state",
                    "field_path": "tests.passing",
                    "check": "equals",
                    "equals": True,
                },
            },
        }
    )
    assert clear is False
    assert condition == "all tests pass"
    assert bounds == {"max_goal_iters": 8}
    assert predicate["field_path"] == "tests.passing"
    # a clear request.
    _c, _b, _p, clear2 = parse_goal_command({"input": "clear"})
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


def test_arm_goal_rejects_bad_predicate(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        raised = False
        try:
            arm_goal(app, sid, condition="c", predicate={"kind": "state"})  # missing field_path
        except GoalError as exc:
            raised = True
            assert exc.reason == "goal_bad_predicate"
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
# Compose with the loop (#1079 loop_goal_met seam)                             #
# =========================================================================== #
def test_loop_goal_satisfied_seam_deterministic_only(tmp_path: Path) -> None:
    def body() -> None:
        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        # NL-only goal: the loop seam cannot cheaply/authoritatively decide -> False.
        arm_goal(app, sid, condition="looks good")
        assert loop_goal_satisfied(app, sid) is False
        # Predicate-backed + satisfied -> the loop seam reports True.
        arm_goal(
            app,
            sid,
            condition="done",
            predicate={"kind": "state", "field_path": "done", "check": "exists", "exists": True},
        )
        assert loop_goal_satisfied(app, sid) is False
        _set_ws(app, sid, {"done": True})
        assert loop_goal_satisfied(app, sid) is True

    _in_ctx(body)


def test_loop_stops_with_loop_goal_met_when_goal_satisfied(tmp_path: Path) -> None:
    """End-to-end compose: an autonomous loop stops with the typed ``loop_goal_met`` reason
    when a predicate-backed goal's deterministic gate holds (the #1079 seam is wired)."""

    def body() -> None:
        from clio_agent.gact.autonomous_loop import loop_wakeup_impl, start_loop

        app = _app(tmp_path)
        sid = _session(app)
        _bind(app, sid)
        start_loop(app, sid, prompt="keep working", interval_s=60, max_iters=100)
        arm_goal(
            app,
            sid,
            condition="done",
            predicate={"kind": "state", "field_path": "done", "check": "exists", "exists": True},
        )
        _set_ws(app, sid, {"done": True})  # the goal's deterministic gate now holds
        app.state.bus.bump(sid, 1.0)  # progress so a stall does not trip first
        result = loop_wakeup_impl(delay_seconds=60, prompt="keep working")
        assert result["stopped"] is True
        loop = app.state.sessions.get(sid).metadata["loop"]
        assert loop["stop_reason"] == "loop_goal_met"

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
        "goal_llm_overridden",
    ):
        assert reason in GOAL_OUTCOME_REASONS


def test_goal_command_row_exists() -> None:
    from clio_agent.gact.runtime.commands import BACKEND_COMMANDS

    row = next((c for c in BACKEND_COMMANDS if c["id"] == "/goal"), None)
    assert row is not None
    assert row["status"] == "available"
    assert row["enabled"] is True


def test_evaluate_goal_returns_goal_decision_type() -> None:
    decision = evaluate_goal(
        {"condition": "c", "iters_elapsed": 0, "max_goal_iters": 3},
        det_result=True,
        llm=GoalJudgement(met=False),
    )
    assert isinstance(decision, GoalDecision)
