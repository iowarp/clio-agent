"""Resume draft selection through the existing Core Question and variant owners."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import dspy

from clio_agent.arc.task_types import (
    QuestionDefinition,
    QuestionReply,
)
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import variant_lines
from clio_agent.gact.agents.variant_close import PENDING_META, close_for_question
from clio_agent.gact.agents.variant_events import emit_selected
from clio_agent.gact.agents.variant_records import TryRecord, VariantRun, load_run, save_run


def _pending_runs(app: Any, sid: str) -> dict[str, Any]:
    """Read a rebuildable continuation projection, including its historical shape."""
    row = app.state.sessions.get(sid)
    pending = (getattr(row, "metadata", None) or {}).get(PENDING_META)
    if not isinstance(pending, Mapping):
        return {}
    if pending.get("variants_id"):
        return {str(pending["variants_id"]): dict(pending)}
    return {str(key): dict(value) for key, value in pending.items() if isinstance(value, Mapping)}


def _clear_pending(app: Any, sid: str, variants_id: str) -> None:
    with app.state.task_history.serialized():
        pending = _pending_runs(app, sid)
        pending.pop(variants_id, None)
        app.state.sessions.update(sid, metadata_patch={PENDING_META: pending})


def _pick(run: VariantRun, question: Any) -> int:
    """The try the user picked (typed error when the answer names none of the drafts)."""
    from clio_agent.gact.agents.variant_drafts import DraftAlternativesError

    chosen = list(question.selected_options or [])[:1]
    for record in run.tries:
        if chosen and record.scope == chosen[0] and record.status == "completed":
            return record.try_index
    raise DraftAlternativesError(f"the answer picks no draft of run {run.variants_id}: {chosen}")


def resume_pending(agent: Any, inputs: Mapping[str, Any]) -> dspy.Prediction | None:
    """Take up this agent's human-judged run once the user answered its question.

    ``None`` when no run waits on this agent. An open Question yields its exact
    handle without launching another run or overwriting the continuation. An answer is
    recorded
    on the run; a Refine comment (below ``n`` tries) runs another try forked from the
    pick and yields again; else BestOfN selects the pick and its line continues the
    conversation -- the forward's answer is the pick's text, with no model call.
    """
    if _ctx.active_react_run() >= 0:
        return None  # a try never resumes anything
    app, sid = _ctx.active_app(), _ctx.active_session_id()
    if app is None:
        return None
    pending = [
        entry
        for entry in _pending_runs(app, sid).values()
        if entry.get("agent_id") == _ctx.active_react_scope()
    ]
    waiting: dspy.Prediction | None = None
    closed: dspy.Prediction | None = None
    selected: list[dspy.Prediction] = []
    for entry in pending:
        run = load_run(app, _ctx.active_react_session() or sid, str(entry["variants_id"]))
        result = resume_question(agent, inputs, run.question_id)
        if result.termination_reason == "variant_pick_yield":
            waiting = result
        elif result.termination_reason == "variant_selected":
            selected.append(result)
        elif result.termination_reason == "variant_closed":
            closed = result
    if selected:
        if len(selected) == 1:
            return selected[0]
        return dspy.Prediction(
            answer="\n\n".join(result.answer for result in selected),
            termination_reason="variant_selected",
            variant_selections=[result.variant_selection for result in selected],
        )
    return waiting or closed


def resume_question(agent: Any, inputs: Mapping[str, Any], handle: str) -> dspy.Prediction:
    """Continue the exact retained choice without retargeting another draft run."""
    from clio_agent.gact.agents.variant_drafts import (
        DRAFT_TOOL,
        DraftAlternativesError,
        _candidate_inputs,
        _commit,
        _Drafts,
        _mark_selected,
        _pause,
        try_module,
    )

    app, sid = _ctx.active_app(), _ctx.active_session_id()
    if app is None:
        raise DraftAlternativesError("Retained draft Question requires its owning application")
    question = app.state.task_history.task_snapshot(handle)
    if question is None:
        raise DraftAlternativesError(
            "Retained draft Question identity is unavailable; recovery required"
        )
    if question.kind != "Question" or question.owner.session_id != sid:
        raise DraftAlternativesError("Retained draft Question belongs to another owner")
    if question.details is None:
        raise DraftAlternativesError("Draft Question posting needs recovery")
    definition = QuestionDefinition.model_validate(
        app.state.task_history.read_content(question.details)
    )
    variants_id = str(definition.metadata.get("variants_id") or "")
    run = load_run(app, sid, variants_id)
    if run.agent_id != _ctx.active_react_scope() or run.question_id != handle:
        raise DraftAlternativesError("Question is not this agent's current draft continuation")
    if run.status == "selected":
        assert run.selected_index is not None
        _clear_pending(app, sid, run.variants_id)
        return dspy.Prediction(
            answer=run.try_at(run.selected_index).text,
            termination_reason="variant_selected",
            variant_selection={
                "variants_id": run.variants_id,
                "variant": run.strategy,
                "judge": run.judge,
                "winning_index": run.selected_index,
                "pick": run.pick,
                "comment": run.comment,
            },
        )
    if question.raw_status == "pending":
        return dspy.Prediction(
            answer="", termination_reason="variant_pick_yield", question_handle=handle
        )
    if question.raw_status != "answered":  # cancelled or expired: closed without a pick
        close_for_question(app, question)
        return dspy.Prediction(
            answer="", termination_reason="variant_closed", question_handle=handle
        )
    if question.result is None:
        raise DraftAlternativesError("Answered draft Question has no confirmed result")
    reply = QuestionReply.model_validate(app.state.task_history.read_content(question.result))
    pick = _pick(run, reply)
    comment = reply.answer.strip()
    run.pick, run.comment, run.question_id, run.status = pick, comment, question.handle, "answered"
    lm = agent.get_lm() or dspy.settings.lm
    drafts = _Drafts(
        run,
        try_module(agent, rubric=run.rubric if run.origin == DRAFT_TOOL else ""),
        _candidate_inputs(inputs, run.rubric) if run.origin == DRAFT_TOOL else inputs,
        lm,
    )
    drafts.save()
    if run.strategy == "refine" and comment and len(run.tries) < run.n:
        source = run.try_at(pick)
        drafts.one_try(
            len(run.tries),
            source=source.scope,
            keep_prefix=len(source.prefix_ids),
            advice=comment,
            forked_from=pick,
        )
        _pause(app, run)
        return dspy.Prediction(
            answer="", termination_reason="variant_pick_yield", question_handle=run.question_id
        )
    winner = drafts.select_pick(pick)
    record = (
        _mark_selected(app, run, winner) if run.origin == DRAFT_TOOL else _commit(app, run, winner)
    )
    _clear_pending(app, sid, run.variants_id)
    return dspy.Prediction(
        answer=record.text,
        termination_reason="variant_selected",
        variant_selection={
            "variants_id": run.variants_id,
            "variant": run.strategy,
            "judge": "user",
            "winning_index": record.try_index,
            "pick": pick,
            "comment": comment,
        },
    )


def variant_answer_problem(question: Any, selected_options: list[str]) -> str | None:
    """Why an answer to a drafts question is invalid (``None``: it is valid).

    The answer is ``selected_options: [<draft id>]`` (the pick) plus an optional
    ``answer`` text (the comment).
    """
    if not (getattr(question, "metadata", None) or {}).get("variants_id"):
        return None
    if len([s for s in selected_options if s]) != 1:
        return "pick exactly one draft (selected_options: [<draft id>]); the answer is a comment"
    return None


def _mark_selected(app: Any, run: VariantRun, index: int) -> TryRecord:
    run.selected_index, run.status = index, "selected"
    save_run(app, run)
    emit_selected(run)
    return run.try_at(index)


def _drafting_note(run: VariantRun) -> str:
    """The ``variant_drafting`` reminder for ``run`` (``""`` when switched off): its
    ``what``, ``capped`` and ``who`` are the ``variant_drafting_*`` pieces."""
    from clio_agent import injections  # noqa: PLC0415

    def piece(name: str, **variables: Any) -> str:
        return injections.render(f"variant_drafting_{name}", variables) or ""

    who = piece("user") if run.judge == "user" else piece("judged")
    if run.strategy == "refine":
        what = piece("refine", n=run.n)
    else:
        what = piece("parallel", n=run.n)
    capped = ""
    if run.n_requested > run.n:
        capped = " " + piece("capped", requested=run.n_requested, n=run.n)
    variables = {"what": what, "capped": capped, "who": who, "rubric": run.rubric}
    return injections.render("variant_drafting", variables) or ""


def _commit(app: Any, run: VariantRun, index: int) -> TryRecord:
    """The selected try's line continues the conversation on the base scope."""
    record = _mark_selected(app, run, index)
    variant_lines.record_winner(record.try_index, record.prefix_ids, cut_id=run.base_cut_id)
    return record


def _selected_result(run: VariantRun) -> str:
    assert run.selected_index is not None
    chosen = run.try_at(run.selected_index)
    score = "" if chosen.score is None else f" (score {chosen.score:.2f})"
    return (
        f"Draft {chosen.try_index + 1} of {len(run.tries)} was selected{score}. It is the "
        "answer the user gets; the turn ends with it."
    )
