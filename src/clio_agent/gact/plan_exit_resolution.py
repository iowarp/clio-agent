"""Plan-exit answer resolution and resume staging."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.plan_exit_resume import approved_plan_resume_text, rejected_plan_resume_text
from clio_agent.gact.plan_review import plan_review_content
from clio_agent.gact.planning import PLAN_VARIANT_METADATA_KEY, transition_playbook_to_execution

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps


def _stage_plan_exit_resume(
    app: "FastAPI",
    deps: "GactDeps",
    sid: str,
    session: Any,
    resume_text: str,
    resume_metadata: dict[str, Any],
    *,
    question_id: str,
) -> None:
    """Resume an approved or rejected plan through the deferred-resume fold."""

    from clio_agent.gact.events import Event
    from clio_agent.gact.loop_inbox import enqueue_user_steer

    if app.state.agent is not None and app.state.turn_runner.busy(sid):
        enqueue_user_steer(
            app,
            sid,
            resume_text,
            {**resume_metadata, "plan_exit_resume": True, "question_id": question_id},
        )
        app.state.bus.publish(
            Event(
                type="plan_exit.resume_deferred",
                session_id=sid,
                payload={"session_id": sid, "question_id": question_id, "reason": "session_busy"},
            )
        )
        return
    if app.state.agent is not None:
        resumed = deps.start_background_user_turn(
            sid,
            session,
            resume_text,
            metadata={**resume_metadata, "plan_exit_resume": True},
            prev_status=str(getattr(session, "status", "waiting_user") or "waiting_user"),
        )
        app.state.bus.publish(
            Event(
                type="plan_exit.resumed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "question_id": question_id,
                    "queued_user_message_id": resumed.id,
                },
            )
        )
        return
    app.state.sessions.update(sid, status="idle")
    app.state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={"session_id": sid, "status": "idle", "prev_status": "waiting_user"},
        )
    )


def _stage_plan_revision_message(
    app: "FastAPI",
    deps: "GactDeps",
    sid: str,
    session: Any,
    feedback: str,
    *,
    plan_file: str,
    question_id: str,
) -> None:
    """Resume rejected plan review with the reviewer's prose as the user message."""

    from clio_agent.gact.events import Event
    from clio_agent.gact.loop_inbox import enqueue_user_steer

    metadata = {
        "behavior": {"execution_mode": "plan"},
        "plan_revision_feedback": True,
        "plan_exit_result": "rejected",
        "plan_file": plan_file,
        "question_id": question_id,
    }
    if app.state.agent is not None and app.state.turn_runner.busy(sid):
        enqueue_user_steer(app, sid, feedback, metadata)
        app.state.bus.publish(
            Event(
                type="plan_exit.revision_deferred",
                session_id=sid,
                payload={"session_id": sid, "question_id": question_id},
            )
        )
        return
    if app.state.agent is not None:
        resumed = deps.start_background_user_turn(
            sid,
            session,
            feedback,
            metadata=metadata,
            prev_status=str(getattr(session, "status", "waiting_user") or "waiting_user"),
        )
        app.state.bus.publish(
            Event(
                type="plan_exit.revision_started",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "question_id": question_id,
                    "user_message_id": resumed.id,
                },
            )
        )
        return
    app.state.sessions.update(sid, status="idle")
    app.state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={"session_id": sid, "status": "idle", "prev_status": "waiting_user"},
        )
    )


def resolve_plan_exit_answer(app: "FastAPI", deps: "GactDeps", sid: str, question: Any) -> None:
    """Apply one explicit plan decision and stage the corresponding lifecycle transition."""

    from clio_agent.gact.events import Event
    from clio_agent.gact.plan_mode import (
        _PLAN_EXIT_CLEAR_CONTEXT,
        _PLAN_EXIT_DECISIONS,
        _PLAN_EXIT_PENDING_KEY,
    )

    session = app.state.sessions.get(sid)
    q_meta = getattr(question, "metadata", None) or {}
    selected = [str(s) for s in (getattr(question, "selected_options", None) or [])]
    answer_meta = getattr(question, "answer_metadata", None) or {}
    plan_file = str(q_meta.get("plan_file") or "")
    decision = next((s for s in selected if s in _PLAN_EXIT_DECISIONS), "reject")
    clear_context = (_PLAN_EXIT_CLEAR_CONTEXT in selected) or bool(answer_meta.get("clear_context"))
    feedback = str(getattr(question, "answer", "") or "").strip()

    app.state.sessions.update(
        sid, metadata_patch={_PLAN_EXIT_PENDING_KEY: {}, "pending_user_question_id": ""}
    )

    if decision == "reject":
        session = app.state.sessions.get(sid)
        app.state.bus.publish(
            Event(
                type="plan_exit.resolved",
                session_id=sid,
                payload={
                    "decision": "reject",
                    "plan_file": plan_file,
                    "composer_user_message": bool(answer_meta.get("composer_user_message")),
                },
            )
        )
        if feedback and answer_meta.get("composer_user_message") is True:
            _stage_plan_revision_message(
                app,
                deps,
                sid,
                session,
                feedback,
                plan_file=plan_file,
                question_id=question.id,
            )
        else:
            _stage_plan_exit_resume(
                app,
                deps,
                sid,
                session,
                rejected_plan_resume_text(feedback, plan_file),
                {"plan_exit_result": "rejected", "plan_file": plan_file},
                question_id=question.id,
            )
        return

    approval_mode = "auto-edits" if decision == "auto" else "ask"
    app.state.sessions.update(
        sid,
        mode="edit",
        approval_mode=approval_mode,
        metadata_patch={PLAN_VARIANT_METADATA_KEY: ""},
    )
    app.state.bus.publish(
        Event(
            type="session.updated",
            session_id=sid,
            payload={
                "session_id": sid,
                "mode": "edit",
                "approval_mode": approval_mode,
                "reason": "plan_exit_approved",
            },
        )
    )
    transition_playbook_to_execution(app, sid)

    from clio_agent.gact.plan_reuse import save_approved_plan

    reviewed_artifact = q_meta.get("artifact_ref")
    saved_plan = (
        dict(reviewed_artifact)
        if isinstance(reviewed_artifact, Mapping) and reviewed_artifact.get("saved") is True
        else save_approved_plan(app, sid, plan_file=plan_file)
    )
    review = {
        "content": str(q_meta.get("plan_content") or ""),
        "content_status": str(q_meta.get("plan_content_status") or ""),
    }
    if not review["content_status"]:
        loaded_review = plan_review_content(plan_file)
        review = {
            "content": str(loaded_review.get("plan_content") or ""),
            "content_status": str(loaded_review.get("plan_content_status") or "unavailable"),
        }
    approved_plan = {"artifact_ref": saved_plan, "plan_file": plan_file, **review}
    cleared = False
    if clear_context:
        app.state.a2ui_store.announce_ledger_clear(sid, "plan_exit_context_cleared")
        deps.replace_session_messages(app, sid, [])
        app.state.sessions.update(
            sid, message_count=0, metadata_patch={"plan_exit_context_cleared": True}
        )
        cleared = True
    session = app.state.sessions.get(sid)
    resume_metadata = {
        "plan_exit_result": "approved",
        "plan_exit_mode": decision,
        "plan_exit_context_cleared": cleared,
        "plan_file": plan_file,
        "approved_plan": approved_plan,
    }
    app.state.bus.publish(
        Event(
            type="plan_exit.resolved",
            session_id=sid,
            payload={
                "decision": decision,
                "cleared_context": cleared,
                "plan_file": plan_file,
                "artifact_ref": saved_plan,
            },
        )
    )

    if decision == "exit_only":
        app.state.sessions.update(
            sid,
            status="idle",
            metadata_patch={"plan_exit_result": "approved_exit_only"},
        )
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={"session_id": sid, "status": "idle", "prev_status": "waiting_user"},
            )
        )
        return

    _stage_plan_exit_resume(
        app,
        deps,
        sid,
        session,
        approved_plan_resume_text(decision, plan_file, approved_plan),
        resume_metadata,
        question_id=question.id,
    )
