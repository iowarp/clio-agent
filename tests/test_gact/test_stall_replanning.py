"""P1.6d #1068 — execution-phase playbook carry + stall-triggered replanning (Magentic-One).

These tests pin the P1.6d contract:

  (1) the leaky bucket ACCUMULATES on stall evidence and DECAYS on progress — a TRANSIENT stall
      never reaches the threshold (hysteresis);
  (2) at the threshold the suggestion fires EXACTLY ONCE, then a cooldown blocks re-firing (sabotage:
      remove the cooldown -> ``test_threshold_fires_once_then_cooldown`` goes red);
  (3) the suggestion is a TYPED INJECTION, never a mode flip (``session.mode`` unchanged);
  (4) execution-phase playbook: a playbook CARRIES into an execution record on plan-exit approve, the
      active step NARROWS tools via the live gate (sabotage the execution-phase read path -> red), and
      the step ADVANCES on the typed write_todos completed-count signal;
  (5) a session without a playbook/plan is byte-identical (the monitor + injection are strict no-ops);
  (6) the bucket state survives ACROSS turns via ``session.metadata`` (including a store reload).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import (
    _make_permission_gate,
    _tool_session_context,
    build_app,
)
from clio_agent.gact.permission_gate import _policy_action_for_tool
from clio_agent.gact.plan_mode import resolve_plan_exit_answer
from clio_agent.gact.planning import (
    PLAYBOOK_EXECUTION_METADATA_KEY,
    Playbook,
    PlaybookStep,
    active_playbook_allowed_tools,
    advance_execution_step,
    parse_playbook,
    record_playbook,
    recorded_execution_playbook,
    recorded_playbook,
)
from clio_agent.gact.replanning import (
    REPLAN_SUGGESTED_EVENT,
    REPLAN_SUGGESTION_KEY,
    REPLAN_SUGGESTION_MARKER,
    STALL_SCORED_EVENT,
    STALL_STATE_KEY,
    STALL_THRESHOLD,
    dispatch_stall_monitor_at_finalize,
    inject_replan_suggestion,
)
from clio_agent.gact.semantic_events import SSE_TRACE_ONLY_EVENT_TYPES
from clio_agent.gact.todos import _write_todos
from tests.test_gact.conftest import complete_turn

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_PLAYBOOK_JSON = (
    '[{"name": "Triage", "tools_allowed": ["fs_read_file"]}, '
    '{"name": "Fix", "tools_allowed": ["fs_apply_edit_write"]}, '
    '{"name": "Verify", "tools_allowed": ["shell_bash"]}]'
)


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #


def _exec_session(tmp_path: Path, *, steps: tuple[PlaybookStep, ...] | None = None):
    """An EDIT-mode session carrying an execution-phase playbook (the monitored case)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    if steps is None:
        steps = (PlaybookStep(name="A"), PlaybookStep(name="B"), PlaybookStep(name="C"))
    pb = Playbook(name="ir", steps=steps)
    app.state.sessions.update(
        sess.id, metadata_patch={PLAYBOOK_EXECUTION_METADATA_KEY: pb.to_metadata()}
    )
    return app, sess.id


def _finalize(app: Any, sid: str, *, tools_called: list[dict[str, Any]] | None = None):
    return dispatch_stall_monitor_at_finalize(
        app, session_id=sid, turn_id="t", tools_called=tools_called or []
    )


def _set_todos(app: Any, sid: str, todos: list[dict[str, str]]) -> None:
    app.state.sessions.update(sid, metadata_patch={"todos": todos})


def _pending(app: Any, sid: str):
    meta = app.state.sessions.get(sid).metadata
    value = meta.get(REPLAN_SUGGESTION_KEY)
    return value if isinstance(value, dict) and value.get("pending") else None


# --------------------------------------------------------------------------- #
# (1) accumulate on stall, decay on progress; transient never fires           #
# --------------------------------------------------------------------------- #


def test_bucket_accumulates_on_stall_and_decays_on_progress(tmp_path: Path) -> None:
    app, sid = _exec_session(tmp_path)
    # stall turn: no todos, no step advance, no tool calls -> +1
    assert _finalize(app, sid)["score"] == 1
    # progress turn: the checklist changed -> -1 -> back to 0
    _set_todos(app, sid, [{"content": "a", "status": "in_progress"}])
    assert _finalize(app, sid)["score"] == 0


def test_transient_stall_never_fires(tmp_path: Path) -> None:
    """A single bad turn between good turns must NEVER reach the threshold (hysteresis)."""
    app, sid = _exec_session(tmp_path)
    for i in range(6):
        if i % 2 == 0:
            # progress turn
            _set_todos(app, sid, [{"content": f"t{i}", "status": "completed"}])
        # else: stall turn (no change)
        st = _finalize(app, sid)
        assert st["score"] < STALL_THRESHOLD
    assert _pending(app, sid) is None  # never fired


def test_repeated_identical_tool_call_is_a_stall(tmp_path: Path) -> None:
    """An identical tool call repeated across turns is a loop (+1) even when the checklist moved."""
    app, sid = _exec_session(tmp_path)
    call = [{"name": "shell_bash", "args": {"cmd": "pytest"}}]
    _set_todos(app, sid, [{"content": "a", "status": "in_progress"}])
    assert _finalize(app, sid, tools_called=call)["score"] == 0  # progress, records the call
    _set_todos(app, sid, [{"content": "a", "status": "completed"}])
    # progress again, BUT the same call repeats -> is_in_loop -> +1 (loop overrides progress).
    assert _finalize(app, sid, tools_called=call)["score"] == 1


# --------------------------------------------------------------------------- #
# (2) threshold fires once then cooldown (cooldown sabotage lock)             #
# --------------------------------------------------------------------------- #


def test_threshold_fires_once_then_cooldown(tmp_path: Path) -> None:
    app, sid = _exec_session(tmp_path)
    _finalize(app, sid)  # score 1
    _finalize(app, sid)  # score 2
    fired = _finalize(app, sid)  # score 3 -> FIRES
    assert fired["score"] == STALL_THRESHOLD
    assert fired["fired_count"] == 1
    assert _pending(app, sid) is not None

    # Simulate the injection consuming the pending flag, then keep stalling.
    app.state.sessions.update(sid, metadata_patch={REPLAN_SUGGESTION_KEY: {}})
    cooled = _finalize(app, sid)  # within cooldown -> must NOT fire again
    # SABOTAGE LOCK: delete the cooldown gate in _run_stall_monitor and this re-fires -> red.
    assert cooled["fired_count"] == 1
    assert _pending(app, sid) is None


# --------------------------------------------------------------------------- #
# (3) the suggestion is a typed injection, NOT a mode flip                    #
# --------------------------------------------------------------------------- #


def test_suggestion_is_injection_not_mode_flip(tmp_path: Path) -> None:
    app, sid = _exec_session(tmp_path)
    for _ in range(STALL_THRESHOLD):
        _finalize(app, sid)
    assert app.state.sessions.get(sid).mode == "edit"  # mode UNCHANGED (no silent flip)
    assert _pending(app, sid) is not None

    out = inject_replan_suggestion(app, sid, app.state.sessions.get(sid), "USER_TEXT")
    assert REPLAN_SUGGESTION_MARKER in out
    assert "planning" in out  # points the model at re-entering plan mode (its decision)
    assert out.endswith("USER_TEXT")
    assert app.state.sessions.get(sid).mode == "edit"  # still edit after the injection

    # Injected EXACTLY ONCE: the flag is cleared, so the next turn is a no-op passthrough.
    assert (
        inject_replan_suggestion(app, sid, app.state.sessions.get(sid), "USER_TEXT") == "USER_TEXT"
    )


def test_scoring_and_suggestion_emit_typed_events(tmp_path: Path, monkeypatch) -> None:
    """No silent scoring: every bucket change emits a typed trace-only event; a fire emits its own."""
    assert STALL_SCORED_EVENT in SSE_TRACE_ONLY_EVENT_TYPES
    assert REPLAN_SUGGESTED_EVENT in SSE_TRACE_ONLY_EVENT_TYPES

    import clio_agent.gact.runtime.globals as g

    events: list[str] = []
    monkeypatch.setattr(
        g, "_emit_semantic_event", lambda app, sid, et, **kw: events.append(et) or {}
    )
    app, sid = _exec_session(tmp_path)
    _finalize(app, sid)
    assert STALL_SCORED_EVENT in events
    _finalize(app, sid)
    _finalize(app, sid)  # fires
    assert REPLAN_SUGGESTED_EVENT in events


# --------------------------------------------------------------------------- #
# (4) execution-phase playbook carry + narrowing + step advancement           #
# --------------------------------------------------------------------------- #


def _fake_deps() -> SimpleNamespace:
    calls: dict[str, list[Any]] = {"resume": [], "replace": []}

    def start_background_user_turn(sid, sess, user_text, *, metadata=None, prev_status="", **kw):
        calls["resume"].append({"sid": sid, "text": user_text})
        return SimpleNamespace(id=f"msg_resume_{len(calls['resume'])}")

    def replace_session_messages(app, sid, messages):
        calls["replace"].append({"sid": sid})

    return SimpleNamespace(
        start_background_user_turn=start_background_user_turn,
        replace_session_messages=replace_session_messages,
        _calls=calls,
    )


def _pending_plan_exit_question(app: Any, sess: Any, *, plan_file: str):
    from clio_agent.gact.plan_mode import PLAN_EXIT_APPROVAL_META, _plan_exit_options
    from clio_agent.gact.types import UserQuestion

    q = UserQuestion(
        id="q_plan_exit",
        session_id=sess.id,
        prompt="approve?",
        status="pending",
        kind="choice",
        options=_plan_exit_options(),
        created_at="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
        source="plan_exit",
        metadata={PLAN_EXIT_APPROVAL_META: True, "resume_on_answer": True, "plan_file": plan_file},
    )
    app.state.user_questions[q.id] = q
    app.state.sessions.update(
        sess.id, status="waiting_user", metadata_patch={"pending_user_question_id": q.id}
    )
    app.state.agent = object()
    return q


def _answer(question: Any, *, selected: list[str], answer: str = ""):
    return question.model_copy(
        update={"status": "answered", "selected_options": selected, "answer": answer}
    )


def test_playbook_carried_into_execution_on_plan_exit_approve(tmp_path: Path) -> None:
    """The P1.6b residual fix: an active playbook does not just clear on approve — it becomes an
    execution record so its per-step tools_allowed keeps narrowing during execution."""
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    record_playbook(app, sess.id, parse_playbook(_PLAYBOOK_JSON))
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})

    q = _pending_plan_exit_question(app, app.state.sessions.get(sess.id), plan_file=str(plan_file))
    resolve_plan_exit_answer(app, _fake_deps(), sess.id, _answer(q, selected=["auto"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"
    assert recorded_playbook(fresh) is None  # the PLAN-phase key is cleared
    execution = recorded_execution_playbook(fresh)
    assert execution is not None  # ... but carried into an EXECUTION record
    assert [s.name for s in execution.steps] == ["Triage", "Fix", "Verify"]
    assert execution.active_step == 0


def test_no_playbook_plan_exit_leaves_no_execution_record(tmp_path: Path) -> None:
    """A plan with no playbook approves cleanly and leaves no stale execution record."""
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n", encoding="utf-8")
    app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})

    q = _pending_plan_exit_question(app, app.state.sessions.get(sess.id), plan_file=str(plan_file))
    resolve_plan_exit_answer(app, _fake_deps(), sess.id, _answer(q, selected=["auto"]))

    assert recorded_execution_playbook(app.state.sessions.get(sess.id)) is None


def test_execution_step_narrows_web_fetch_via_live_gate(tmp_path: Path) -> None:
    """PRIMARY sabotage lock: in EDIT mode the active execution step's allowlist narrows a tool the
    gate would otherwise allow. The deny is caused SOLELY by the execution-phase read path
    (active_playbook_allowed_tools -> recorded_execution_playbook); severing it flips this back to
    allow and turns the test red."""
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    sid = sess.id
    # A session-scoped allow-all so the gate resolves cleanly (no interactive park on "ask").
    app.state.permission_policies = [
        {
            "kind": "tool",
            "action": "allow",
            "tool_name_pattern": "*",
            "scope": "session",
            "scope_id": sid,
        }
    ]
    gate = _make_permission_gate(app)

    # Control: NO execution playbook -> web_fetch is allowed.
    with _tool_session_context(sid):
        assert gate("web_fetch", {}) == "allow"

    step = PlaybookStep(name="impl", tools_allowed=("fs_read_file",))
    app.state.sessions.update(
        sid,
        metadata_patch={
            PLAYBOOK_EXECUTION_METADATA_KEY: Playbook(name="ir", steps=(step,)).to_metadata()
        },
    )

    # Now the SAME live gate DENIES web_fetch — solely because the execution step narrows it.
    with _tool_session_context(sid):
        assert gate("web_fetch", {}) == "deny"
    # ... and a tool IN the step allowlist is not narrowed.
    with _tool_session_context(sid):
        assert gate("fs_read_file", {}) != "deny"


def test_active_step_narrowing_follows_advancement(tmp_path: Path) -> None:
    """After the step advances, the allowlist that narrows is the NEW active step's (execution read
    path is step-aware, not frozen at step 0 — the P1.6b residual)."""
    steps = (
        PlaybookStep(name="Triage", tools_allowed=("fs_read_file",)),
        PlaybookStep(name="Fix", tools_allowed=("fs_apply_edit_write",)),
    )
    app, sid = _exec_session(tmp_path, steps=steps)
    # step 0: allowlist is the Triage step's
    assert active_playbook_allowed_tools(app.state.sessions.get(sid)) == ("fs_read_file",)
    # advance to step 1 (one completed todo)
    assert advance_execution_step(app, sid, completed_todos=1) == 1
    # narrowing now follows the Fix step
    assert active_playbook_allowed_tools(app.state.sessions.get(sid)) == ("fs_apply_edit_write",)


def test_step_advances_on_write_todos_completed_count(tmp_path: Path) -> None:
    """The typed step-advancement signal is the write_todos completed count: marking todos complete
    advances the execution active step (clamped, forward-only) through the REAL tool path."""
    steps = (PlaybookStep(name="A"), PlaybookStep(name="B"), PlaybookStep(name="C"))
    app, sid = _exec_session(tmp_path, steps=steps)

    # one completed todo -> active step 1 (distinct turn id so the same-step guard does not fire)
    tok = _ctx.set_turn_id_token("turn_1")
    try:
        _write_todos(
            app,
            sid,
            app.state.sessions.get(sid),
            [{"content": "x", "status": "completed"}, {"content": "y", "status": "in_progress"}],
        )
    finally:
        _ctx.reset(tok)
    assert recorded_execution_playbook(app.state.sessions.get(sid)).active_step == 1

    # more completed than steps -> clamped to the last step (index 2)
    tok = _ctx.set_turn_id_token("turn_2")
    try:
        _write_todos(
            app,
            sid,
            app.state.sessions.get(sid),
            [{"content": c, "status": "completed"} for c in ("x", "y", "z", "w")],
        )
    finally:
        _ctx.reset(tok)
    assert recorded_execution_playbook(app.state.sessions.get(sid)).active_step == 2


def test_advance_is_forward_only(tmp_path: Path) -> None:
    steps = (PlaybookStep(name="A"), PlaybookStep(name="B"), PlaybookStep(name="C"))
    app, sid = _exec_session(tmp_path, steps=steps)
    assert advance_execution_step(app, sid, completed_todos=2) == 2
    # a later write with FEWER completed never moves the step backward.
    assert advance_execution_step(app, sid, completed_todos=1) is None
    assert recorded_execution_playbook(app.state.sessions.get(sid)).active_step == 2


def test_advance_is_noop_without_execution_playbook(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    assert advance_execution_step(app, sess.id, completed_todos=3) is None


# --------------------------------------------------------------------------- #
# (5) sessions without a playbook/plan are byte-identical (golden)            #
# --------------------------------------------------------------------------- #


def test_plain_session_monitor_and_injection_are_noops(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    before = dict(app.state.sessions.get(sess.id).metadata)

    result = _finalize(app, sess.id, tools_called=[{"name": "x", "args": {}}])
    assert result is None  # no-op for an unstructured session
    after = dict(app.state.sessions.get(sess.id).metadata)
    assert after == before  # NO metadata churn (golden byte-identical)
    assert STALL_STATE_KEY not in after

    # the injection is a strict passthrough too
    assert inject_replan_suggestion(app, sess.id, app.state.sessions.get(sess.id), "X") == "X"


def test_plan_mode_session_is_not_scored_and_resets(tmp_path: Path) -> None:
    """In plan mode the monitor is a no-op AND resets any accumulated bucket (plan re-entry)."""
    app, sid = _exec_session(tmp_path)
    _finalize(app, sid)
    _finalize(app, sid)  # score 2
    assert app.state.sessions.get(sid).metadata[STALL_STATE_KEY]["score"] == 2

    app.state.sessions.update(sid, mode="plan")  # the session re-enters plan mode
    assert _finalize(app, sid) is None  # no scoring while planning
    assert not app.state.sessions.get(sid).metadata.get(STALL_STATE_KEY)  # bucket reset


# --------------------------------------------------------------------------- #
# (6) bucket state survives across turns via session.metadata                 #
# --------------------------------------------------------------------------- #


def test_bucket_state_persists_across_turns_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    app = build_app(sessions_path=path)
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    pb = Playbook(name="ir", steps=(PlaybookStep(name="A"), PlaybookStep(name="B")))
    app.state.sessions.update(
        sess.id, metadata_patch={PLAYBOOK_EXECUTION_METADATA_KEY: pb.to_metadata()}
    )
    _finalize(app, sess.id)  # score 1
    assert app.state.sessions.get(sess.id).metadata[STALL_STATE_KEY]["score"] == 1

    # A fresh app over the SAME store reloads the persisted bucket and continues from it.
    app2 = build_app(sessions_path=path)
    st = dispatch_stall_monitor_at_finalize(app2, session_id=sess.id, tools_called=[])
    assert st["score"] == 2  # carried across the reload via session.metadata
    assert st["turns"] == 2


# --------------------------------------------------------------------------- #
# helper-visible: _policy_action shim also honours the execution read path     #
# --------------------------------------------------------------------------- #


def test_execution_narrowing_through_policy_action_shim(tmp_path: Path) -> None:
    """The ``_policy_action_for_tool`` shim (permission_gate.py:256) also narrows off the execution
    record — a second consult site of the execution-phase read path."""
    step = PlaybookStep(name="impl", tools_allowed=("fs_read_file",))
    app, sid = _exec_session(tmp_path, steps=(step,))
    fresh = app.state.sessions.get(sid)
    assert (
        _policy_action_for_tool(
            app, session_id=sid, session=fresh, tool_name="web_fetch", args={}, mode="edit"
        )
        == "deny"
    )


# --------------------------------------------------------------------------- #
# (7) LIVE WIRING: the two shipped call sites are pinned through a REAL turn    #
#     (repair pass 1 — the direct-call unit tests above leave the finalize      #
#      hook and the enrichment injection deletable without a red test).         #
# --------------------------------------------------------------------------- #


class _Pred:
    """A canned turn prediction the host fake returns (mirrors the composed-governance fake)."""

    answer = "ok"
    selected_expert = "none"
    routing_rationale = ""
    tools_called: list = []
    tokens: dict = {}
    cost_usd = 0.0
    file_diffs: list = []
    permissions_requested: list = []
    nanoagents_spawned: list = []


class _Agent:
    """A host fake that returns a canned prediction for a driven turn."""

    def forward(self, *args: Any, **kwargs: Any) -> _Pred:
        return _Pred()


class _QuestionRecordingAgent:
    """A host fake whose ``forward`` records the composed turn input (``question``) it is handed.

    The turn engine passes ``state.enriched_text`` — the fully composed, post-enrichment turn
    input — as ``question``. Recording it lets a test assert what actually reached the model input
    after ``inject_replan_suggestion`` ran at the real enrichment call site.
    """

    def __init__(self) -> None:
        self.questions: list[str] = []

    def forward(self, question: str, session_id: str, **kwargs: Any) -> _Pred:
        self.questions.append(question)
        return _Pred()


def _arm_execution_playbook(app: Any, sid: str) -> None:
    """Put ``sid`` into EDIT mode carrying an execution-phase playbook (the monitored case)."""

    pb = Playbook(name="ir", steps=(PlaybookStep(name="A"), PlaybookStep(name="B")))
    app.state.sessions.update(
        sid, mode="edit", metadata_patch={PLAYBOOK_EXECUTION_METADATA_KEY: pb.to_metadata()}
    )


def test_finalize_hook_scores_bucket_through_real_turn(tmp_path: Path) -> None:
    """WIRING LOCK (finalize): a whole turn driven through the shipped ``finalize_turn`` path scores
    the leaky bucket on an execution-playbook session.

    Pins ``turn_finalize.finalize_turn``'s ``dispatch_stall_monitor_at_finalize`` call site: the
    stall state only appears if the real finalize hook ran. SABOTAGE: delete that call site and
    ``STALL_STATE_KEY`` never lands -> this test goes RED (the unit tests above call the hook
    directly and would stay green, which is exactly the gap the reviewer flagged)."""

    from fastapi.testclient import TestClient

    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent())) as client:
        sid = client.post("/v1/sessions", json={"title": "finalize-wiring"}).json()["id"]
        _arm_execution_playbook(client.app, sid)

        before = client.app.state.sessions.get(sid).metadata.get(STALL_STATE_KEY)
        assert not before  # no bucket before any turn

        complete_turn(client, sid, "do the work")

        state = client.app.state.sessions.get(sid).metadata.get(STALL_STATE_KEY)
        assert isinstance(state, dict), (
            "the finalize stall-monitor hook did not run on the real turn"
        )
        assert state["turns"] == 1  # exactly one finalized turn was scored
        assert state["score"] == 1  # a no-progress turn stalled the bucket by one


def test_enrichment_injection_reaches_real_turn_input(tmp_path: Path) -> None:
    """WIRING LOCK (enrichment): a pending replan suggestion reaches the COMPOSED turn input of a
    real turn.

    Pins ``turn.py``'s ``inject_replan_suggestion`` enrichment call site: with the pending flag
    seeded, the marker must appear in the ``question`` the model input received. SABOTAGE: delete
    that call site and the marker never reaches the turn input -> this test goes RED (the unit test
    above calls ``inject_replan_suggestion`` directly and would stay green)."""

    from fastapi.testclient import TestClient

    agent = _QuestionRecordingAgent()
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = client.post("/v1/sessions", json={"title": "enrich-wiring"}).json()["id"]
        # Edit mode (not plan) + a pending suggestion the enrichment step must inject exactly once.
        client.app.state.sessions.update(
            sid,
            mode="edit",
            metadata_patch={REPLAN_SUGGESTION_KEY: {"pending": True, "score": 3, "turn_id": "t"}},
        )

        complete_turn(client, sid, "USER_TEXT")

        assert agent.questions, "the host agent forward never ran on the real turn"
        assert any(REPLAN_SUGGESTION_MARKER in q for q in agent.questions), (
            "the replan suggestion never reached the composed turn input"
        )
        assert any("USER_TEXT" in q for q in agent.questions)  # the user's text is still carried
        # Injected EXACTLY ONCE: the pending flag is consumed by the real enrichment pass.
        assert not _pending(client.app, sid)


def test_finalize_and_enrichment_compose_end_to_end(tmp_path: Path) -> None:
    """WIRING LOCK (both): across real turns the finalize hook accumulates the bucket to threshold
    and the NEXT turn's real enrichment injects the resulting suggestion — both shipped call sites
    exercised together, no direct hook/injection calls."""

    from fastapi.testclient import TestClient

    agent = _QuestionRecordingAgent()
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = client.post("/v1/sessions", json={"title": "e2e-wiring"}).json()["id"]
        _arm_execution_playbook(client.app, sid)

        # Each no-progress turn stalls +1 through the REAL finalize hook; at threshold it fires and
        # seeds the pending suggestion (no direct dispatch calls).
        for _ in range(STALL_THRESHOLD):
            complete_turn(client, sid, "keep working")
        state = client.app.state.sessions.get(sid).metadata[STALL_STATE_KEY]
        assert state["score"] == STALL_THRESHOLD and state["fired_count"] == 1
        assert _pending(client.app, sid) is not None

        # The FOLLOWING real turn's enrichment injects the fired suggestion into the model input.
        before = len(agent.questions)
        complete_turn(client, sid, "next")
        assert any(REPLAN_SUGGESTION_MARKER in q for q in agent.questions[before:]), (
            "the fired suggestion never reached a subsequent real turn's input"
        )
