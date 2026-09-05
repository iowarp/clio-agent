"""P1.4 #1066 — plan_exit tool + N-way approval + constraint-lift + durable defer.

Covers the plan-exit turn-ending yield end to end (minus the live model): the tool's two hard-error
guardrails, the post-forward yield seam that mints the N-way approval question, and
``resolve_plan_exit_answer`` applying each decision (auto / interactive / exit_only / clear-context
modifier / reject) plus the durable-defer resume that rides the #1031 deferred-resume fold.

The plan_acl allow-band (plan_exit / ask_user / web_fetch resolve ALLOW in plan mode, writes stay
DENIED) is proven in ``test_grant_resolver.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.plan_mode import (
    PLAN_EXIT_APPROVAL_META,
    PlanExitError,
    _plan_exit_options,
    build_plan_exit_tool,
    maybe_pause_for_plan_exit,
    resolve_plan_exit_answer,
)
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID
from clio_agent.gact.types import UserQuestion


def _make_app(tmp_path: Path):
    return build_app(sessions_path=tmp_path / "s.json")


def _plan_session(app: Any, tmp_path: Path, *, mode: str = "plan", with_plan_file: bool = True):
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode=mode)
    if with_plan_file:
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n- do a thing\n", encoding="utf-8")
        app.state.sessions.update(sess.id, metadata_patch={"plan_file": str(plan_file)})
    return app.state.sessions.get(sess.id)


# --------------------------------------------------------------------------- #
# The tool's two hard-error guardrails
# --------------------------------------------------------------------------- #


def _call_plan_exit(app: Any, sid: str, **kwargs: Any) -> str:
    tool = build_plan_exit_tool(SimpleNamespace(id="main"))
    token_app = _ctx.set_app(app)
    token_sid = _ctx.set_session_id(sid)
    try:
        return tool.func(**kwargs)
    finally:
        _ctx.reset(token_sid)
        _ctx.reset(token_app)


def test_plan_exit_no_plan_file_hard_errors_naming_path(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path, with_plan_file=False)
    with pytest.raises(PlanExitError) as exc:
        _call_plan_exit(app, sess.id, summary="done")
    msg = str(exc.value)
    assert "no plan file exists at" in msg
    # The message names the expected plans-dir path (the model can act on it).
    from clio_agent.gact.runtime.grant_resolver import plans_dir

    assert str(plans_dir()) in msg
    # Mode is unchanged and no pending request was stashed.
    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"
    assert not fresh.metadata.get("pending_plan_exit")


def test_plan_exit_outside_plan_mode_errors(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path, mode="edit")
    with pytest.raises(PlanExitError, match="only available in plan mode"):
        _call_plan_exit(app, sess.id, summary="done")
    assert app.state.sessions.get(sess.id).mode == "edit"


def test_plan_exit_requires_summary(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    with pytest.raises(PlanExitError, match="requires a 1-2 sentence 'summary'"):
        _call_plan_exit(app, sess.id, summary="   ")


def test_plan_exit_rejects_bad_recommended_mode(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    with pytest.raises(PlanExitError, match="recommendedMode must be one of"):
        _call_plan_exit(app, sess.id, summary="ok", recommendedMode="whatever")


def test_plan_exit_success_records_pending_request(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    out = _call_plan_exit(app, sess.id, summary="ship it", recommendedMode="auto", riskNotes="none")
    assert "handed back to the user" in out
    pending = app.state.sessions.get(sess.id).metadata.get("pending_plan_exit")
    assert pending["summary"] == "ship it"
    assert pending["recommended_mode"] == "auto"
    assert pending["surfaced"] is False


# --------------------------------------------------------------------------- #
# The post-forward yield seam mints the N-way approval question
# --------------------------------------------------------------------------- #


def _fake_state(app: Any, sess: Any) -> SimpleNamespace:
    published: list[Any] = []
    bus = SimpleNamespace(publish=published.append)
    state = SimpleNamespace(
        app=app,
        sid=sess.id,
        error_info=None,
        turn_id="turn_1",
        trace_id="trace_1",
        selected_agent="main",
        invocation_agent_id="main",
        retry_attempt_id="",
        user_msg=SimpleNamespace(id="msg_user_1"),
        context_frame={"id": "cf_1"},
        bus=bus,
    )
    state._published = published  # type: ignore[attr-defined]
    return state


def test_maybe_pause_mints_approval_question_and_yields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    _call_plan_exit(app, sess.id, summary="ship it", recommendedMode="auto")

    monkeypatch.setattr("clio_agent.gact.turn_stream.settle_turn_transcript", lambda state: None)
    monkeypatch.setattr(
        "clio_agent.gact.enrichment._finalize_context_frame",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "clio_agent.gact.runtime.globals._emit_semantic_event", lambda *a, **k: None
    )

    state = _fake_state(app, sess)
    assert maybe_pause_for_plan_exit(state) is True

    # Exactly one plan-exit approval question was minted, pending, N-way.
    questions = [
        q for q in app.state.user_questions.values() if q.metadata.get(PLAN_EXIT_APPROVAL_META)
    ]
    assert len(questions) == 1
    q = questions[0]
    assert q.status == "pending"
    assert q.metadata["plan_content"] == "# Plan\n- do a thing\n"
    assert q.metadata["plan_content_status"] == "complete"
    assert {o.value for o in q.options} >= {
        "auto",
        "interactive",
        "exit_only",
        "reject",
        "clear_context",
    }
    # Session flipped to waiting_user; the request is marked surfaced (never re-minted).
    fresh = app.state.sessions.get(sess.id)
    assert fresh.status == "waiting_user"
    assert fresh.metadata["pending_plan_exit"]["surfaced"] is True

    from clio_agent.gact.routes.interactions import project_pending_interactions

    projection = project_pending_interactions(app, sess.id, include_children=False)
    [interaction] = projection.rows
    assert interaction.title == "Review execution plan"
    assert interaction.source.tool_name == "plan_exit"
    assert interaction.payload["plan_exit"] == {
        "summary": "ship it",
        "recommended_mode": "auto",
        "plan_file": str(tmp_path / "plan.md"),
        "plan_content": "# Plan\n- do a thing\n",
        "plan_content_status": "complete",
    }

    # A second seam call is a no-op (already surfaced) — no double question.
    assert maybe_pause_for_plan_exit(state) is False
    assert (
        len(
            [
                q
                for q in app.state.user_questions.values()
                if q.metadata.get(PLAN_EXIT_APPROVAL_META)
            ]
        )
        == 1
    )


def test_maybe_pause_noop_without_pending(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    assert maybe_pause_for_plan_exit(_fake_state(app, sess)) is False


# --------------------------------------------------------------------------- #
# resolve_plan_exit_answer — each decision + the clear-context modifier + durable defer
# --------------------------------------------------------------------------- #


def _fake_deps() -> SimpleNamespace:
    calls: dict[str, list[Any]] = {"resume": [], "replace": []}

    def start_background_user_turn(sid, sess, user_text, *, metadata=None, prev_status="", **kw):
        calls["resume"].append({"sid": sid, "text": user_text, "metadata": metadata or {}})
        return SimpleNamespace(id=f"msg_resume_{len(calls['resume'])}")

    def replace_session_messages(app, sid, messages):
        calls["replace"].append({"sid": sid, "messages": messages})

    return SimpleNamespace(
        start_background_user_turn=start_background_user_turn,
        replace_session_messages=replace_session_messages,
        _calls=calls,
    )


def _pending_plan_exit_question(app: Any, sess: Any, *, plan_file: str) -> UserQuestion:
    q = UserQuestion(
        id="q_plan_exit",
        session_id=sess.id,
        prompt="approve?",
        status="pending",
        kind="choice",
        options=_plan_exit_options(),
        created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
        source="plan_exit",
        metadata={
            PLAN_EXIT_APPROVAL_META: True,
            "resume_on_answer": True,
            "plan_file": plan_file,
            "recommended_mode": "auto",
        },
    )
    app.state.user_questions[q.id] = q
    app.state.sessions.update(
        sess.id, status="waiting_user", metadata_patch={"pending_user_question_id": q.id}
    )
    # A resume needs an executable agent present (the deferred-resume entrypoint gates on it).
    app.state.agent = object()
    return q


def _answer(question: UserQuestion, *, selected: list[str], answer: str = "") -> UserQuestion:
    return question.model_copy(
        update={"status": "answered", "selected_options": selected, "answer": answer}
    )


def test_approve_auto_transitions_edit_and_injects_constraint_lift(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    plan_file = str(tmp_path / "plan.md")
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=plan_file)
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"
    assert fresh.approval_mode == "auto-edits"
    assert len(deps._calls["resume"]) == 1
    resume = deps._calls["resume"][0]
    assert "[STATE TRANSITION OVERRIDE]" in resume["text"]
    assert plan_file in resume["text"]
    assert "Begin implementing the approved plan now." in resume["text"]
    assert resume["metadata"]["plan_exit_result"] == "approved"
    # The pending-request bookkeeping is cleared.
    assert not fresh.metadata.get("pending_plan_exit")


def test_approve_interactive_uses_ask_approval_mode(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=str(tmp_path / "plan.md"))
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["interactive"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"
    assert fresh.approval_mode == "ask"
    assert "prompted to approve each action" in deps._calls["resume"][0]["text"]


def test_approve_exit_only_leaves_plan_but_does_not_execute(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=str(tmp_path / "plan.md"))
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["exit_only"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"  # left plan mode
    assert fresh.status == "idle"
    # NO resume turn, NO execute-now message — the model must not start editing.
    assert deps._calls["resume"] == []


def test_approve_clear_context_modifier_applies(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=str(tmp_path / "plan.md"))
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto", "clear_context"]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "edit"
    assert fresh.metadata.get("plan_exit_context_cleared") is True
    assert len(deps._calls["replace"]) == 1  # history was cleared
    assert deps._calls["replace"][0]["messages"] == []
    assert deps._calls["resume"][0]["metadata"]["plan_exit_context_cleared"] is True


def test_reject_stays_in_plan_mode_with_feedback(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    plan_file = str(tmp_path / "plan.md")
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=plan_file)
    deps = _fake_deps()

    resolve_plan_exit_answer(
        app, deps, sess.id, _answer(q, selected=["reject"], answer="add a rollback section")
    )

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"  # stayed in plan mode
    resume = deps._calls["resume"][0]
    assert "REJECTED" in resume["text"]
    assert "add a rollback section" in resume["text"]  # feedback visible
    assert plan_file in resume["text"]  # rejected plan referenced
    assert resume["metadata"]["plan_exit_result"] == "rejected"


def test_empty_selection_rejects_safe_ignoring_recommended_mode(tmp_path: Path) -> None:
    """A decisionless approval (no option selected) must reject-safe, NEVER substitute the model's
    recommended_mode. Otherwise a plan the human never explicitly approved would auto-execute."""
    app = _make_app(tmp_path)
    plan_file = str(tmp_path / "plan.md")
    sess = _plan_session(app, tmp_path)
    # recommended_mode="auto" is baked into the question metadata by _pending_plan_exit_question.
    q = _pending_plan_exit_question(app, sess, plan_file=plan_file)
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=[]))

    fresh = app.state.sessions.get(sess.id)
    assert fresh.mode == "plan"  # stayed in plan mode despite recommended_mode="auto"
    resume = deps._calls["resume"][0]
    assert "REJECTED" in resume["text"]
    assert "[STATE TRANSITION OVERRIDE]" not in resume["text"]  # no execute-now constraint-lift
    assert resume["metadata"]["plan_exit_result"] == "rejected"


def test_durable_defer_resume_when_turn_already_ended(tmp_path: Path) -> None:
    """Approval arriving after the turn ended (session idle/waiting_user, not busy) resumes as a
    new turn via start_background_user_turn — the #1031 deferred-resume guarantee."""
    app = _make_app(tmp_path)
    plan_file = str(tmp_path / "plan.md")
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=plan_file)
    deps = _fake_deps()
    # Not busy (the turn ended), agent present — the deferred-resume path.
    assert not app.state.turn_runner.busy(sess.id)
    app.state.agent = object()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto"]))

    assert len(deps._calls["resume"]) == 1  # resumed as ONE new turn
    assert app.state.sessions.get(sess.id).mode == "edit"  # transition fired on resume
    assert "[STATE TRANSITION OVERRIDE]" in deps._calls["resume"][0]["text"]  # constraints lifted


def _mint_and_resolve(
    app: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, decision: str, answer: str = ""
) -> tuple[Any, int]:
    """Record → seam-mint an approval question → resolve it with ``decision``.

    Returns the plan session and the count of plan-exit approval questions minted before the
    post-resolution seam is driven again (the phantom-re-approval regression setup).
    """

    sess = _plan_session(app, tmp_path)
    _call_plan_exit(app, sess.id, summary="ship it", recommendedMode="auto")

    monkeypatch.setattr("clio_agent.gact.turn_stream.settle_turn_transcript", lambda state: None)
    monkeypatch.setattr("clio_agent.gact.enrichment._finalize_context_frame", lambda *a, **k: None)
    monkeypatch.setattr(
        "clio_agent.gact.runtime.globals._emit_semantic_event", lambda *a, **k: None
    )

    assert maybe_pause_for_plan_exit(_fake_state(app, sess)) is True
    q = next(
        q for q in app.state.user_questions.values() if q.metadata.get(PLAN_EXIT_APPROVAL_META)
    )

    deps = _fake_deps()
    app.state.agent = object()
    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=[decision], answer=answer))

    minted = len(
        [q for q in app.state.user_questions.values() if q.metadata.get(PLAN_EXIT_APPROVAL_META)]
    )
    return sess, minted


def test_seam_no_phantom_reapproval_after_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an APPROVED plan-exit, the resolved-tombstone ``pending_plan_exit == {}`` must read as
    absent: a fresh next-turn seam call returns False and mints NO second (phantom) approval question.

    Regression for the plan-exit phantom re-approval BLOCKER (plan_mode.py:483/707): the resolve
    tombstone writes ``{}``, which an ``isinstance(pending, Mapping)``-only guard treated as a live,
    un-surfaced request and re-surfaced — hijacking the resumed execution turn with a second approval.
    """

    app = _make_app(tmp_path)
    sess, minted = _mint_and_resolve(app, tmp_path, monkeypatch, decision="auto")
    assert minted == 1  # only the original approval question exists

    # The resume turn runs the seam again against the {} tombstone: it must not re-surface.
    assert maybe_pause_for_plan_exit(_fake_state(app, sess)) is False
    after = len(
        [q for q in app.state.user_questions.values() if q.metadata.get(PLAN_EXIT_APPROVAL_META)]
    )
    assert after == minted  # no phantom re-approval


def test_seam_no_phantom_reapproval_after_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a REJECTED plan-exit, the seam must likewise not re-surface a phantom question on the
    feedback-revision turn — the worst path, where a phantom approval would hijack the revision.
    """

    app = _make_app(tmp_path)
    sess, minted = _mint_and_resolve(
        app, tmp_path, monkeypatch, decision="reject", answer="add a rollback section"
    )
    assert minted == 1

    assert maybe_pause_for_plan_exit(_fake_state(app, sess)) is False
    after = len(
        [q for q in app.state.user_questions.values() if q.metadata.get(PLAN_EXIT_APPROVAL_META)]
    )
    assert after == minted  # no phantom re-approval on the revision turn
    assert app.state.sessions.get(sess.id).mode == "plan"  # reject stayed in plan mode


def test_durable_defer_busy_folds_into_loop_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a turn is somehow in flight, the resume folds into the loop inbox (never dropped)."""
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    q = _pending_plan_exit_question(app, sess, plan_file=str(tmp_path / "plan.md"))
    deps = _fake_deps()
    app.state.agent = object()
    monkeypatch.setattr(app.state.turn_runner, "busy", lambda sid: True)  # mark busy

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto"]))

    # No direct background turn — the resume rode the loop inbox fold instead.
    assert deps._calls["resume"] == []
    inbox = app.state.loop_inboxes.get(sess.id)
    assert inbox is not None and inbox.peek_nonempty()
    assert app.state.sessions.get(sess.id).mode == "edit"  # transition still fired


def test_clear_context_announces_every_surface_it_deletes(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    sess = _plan_session(app, tmp_path)
    app.state.a2ui_store.apply_batch(
        sess.id,
        [
            {
                "version": "v0.9.1",
                "createSurface": {
                    "surfaceId": "plan_surface",
                    "catalogId": CLIO_A2UI_CATALOG_ID,
                },
            },
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "plan_surface",
                    "components": [
                        {
                            "id": "root",
                            "component": "clio.status.v1",
                            "label": "Plan",
                            "state": "completed",
                        }
                    ],
                },
            },
        ],
    )
    q = _pending_plan_exit_question(app, sess, plan_file=str(tmp_path / "plan.md"))
    deps = _fake_deps()

    resolve_plan_exit_answer(app, deps, sess.id, _answer(q, selected=["auto", "clear_context"]))

    assert deps._calls["replace"][0]["messages"] == []
    deletions = [
        event.payload
        for event in app.state.bus._history[sess.id]
        if event.type == "a2ui.surface.deleted"
    ]
    assert deletions == [
        {"surface_id": "plan_surface", "reason": "plan_exit_context_cleared"},
    ]
