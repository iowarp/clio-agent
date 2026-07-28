"""Plan-mode reminder attachment + plan-file lifecycle — the per-turn plan-mode contract.

Owner module for the plan-mode PROMPT surface (P1.2 #1064) and the plan-file lifecycle
(P1.3 #1065). ``session.mode`` used to be plumbed to every agent ``forward()`` and then
discarded (``builders.py``), so the model got ZERO signal it was in plan mode — and any signal
placed in the system prompt is lost once the KV-cache prefix is compacted. This module
re-injects the plan-mode contract into the model's *turn input* each turn (exactly like
``enrichment.inject_pending_agent_task_notifications`` re-injects observe-later results), so it
survives compaction without invalidating the prefix.

**Plan-file lifecycle (P1.3 #1065).** clio owns the plan file's PATH, EXISTENCE, and GUIDANCE —
the MODEL authors its content. On the first plan-mode turn a deterministic path
(:func:`_compute_plan_file_path`) is computed under ``grant_resolver.plans_dir()`` (a slug of the
session's first user prompt/title, session-id-suffixed for per-session uniqueness) and RECORDED on
``session.metadata`` (:data:`_PLAN_FILE_METADATA_KEY`) via ``sessions.update(metadata_patch=…)`` —
the #948 ``AgentTask`` no-fifth-store projection pattern, NOT ``workflow_state``, NOT a new store.
The recorded path always falls within the P1.1 plan-ACL carve-out glob (``<plans>/*.md``) so the
model can actually write it via ``fs_apply_edit_write`` (asserted at computation). Because the path
is recorded once and re-read thereafter, it is STABLE across turns regardless of derivation source.
clio never pre-writes the file's content; the reminder only points the model at the path.

Kept LEAN so plan mode never bloats context: a FULL reminder (create-vs-edit branch, adaptive
structure hint, epistemic-ledger headers, staleness + show-the-plan rules) is injected only on the
first plan turn, immediately after a compaction (which drops the earlier one from the model's
view), and once per :data:`_PLAN_REMINDER_FULL_INTERVAL` turns; every turn in between carries a
single-line marker. The tiny suppression counter lives on ``session.metadata`` alongside the plan
path.

The block is SERVER grounding prepended to the turn input — never user text, never model output
— so it carries a stable, greppable marker (:data:`PLAN_MODE_REMINDER_MARKER`, the #881 marker
discipline). ``turn.py`` calls :func:`inject_plan_mode_reminder` from its enrichment step.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps
    from clio_agent.gact.turn_state import TurnState

#: Marker heading the plan-mode reminder block (stable + greppable; #881 discipline).
PLAN_MODE_REMINDER_MARKER = "## Plan Mode active — read-only except the plan file"

#: Re-inject the FULL reminder at most once per this many plan-mode turns (a sparse one-liner
#: in between); a compaction forces a full re-inject immediately regardless of the window.
_PLAN_REMINDER_FULL_INTERVAL = 10

#: ``session.metadata`` key holding the tiny per-session suppression counter (no fifth store).
_PLAN_REMINDER_STATE_KEY = "plan_mode_reminder"

#: ``session.metadata`` key holding the deterministic plan-file path (P1.3 #1065; no fifth store).
_PLAN_FILE_METADATA_KEY = "plan_file"

#: Cap the prompt/title-derived slug component so a long first prompt cannot bloat the filename.
_PLAN_SLUG_MAX_LEN = 60


def _session_compaction_count(app: "FastAPI", sid: str) -> int:
    """Return how many compaction summaries are in this session's live transcript.

    A compaction (``POST /v1/sessions/{sid}/compact`` or the auto path) appends an assistant
    message carrying a single ``compaction`` part (SPEC §4.5). Counting those parts gives a
    monotonic "a compaction happened since the last full plan reminder" signal — the trigger to
    re-inject the full contract (it would otherwise have been compacted out of the model's view).
    """

    count = 0
    for msg in app.state.messages.get(sid, []) or []:
        for part in getattr(msg, "parts", []) or []:
            if getattr(part, "type", "") == "compaction":
                count += 1
    return count


def _slugify(text: str) -> str:
    """Return a filesystem-safe slug (lowercase alnum + hyphens) of ``text`` (empty if none).

    Collapses every non-alphanumeric run to a single hyphen and trims edge hyphens, so the result
    can carry NO path separator, ``..``, or extension dot — the property that keeps a derived plan
    filename provably inside the plans dir (it cannot traverse out of the carve-out).
    """

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:_PLAN_SLUG_MAX_LEN].strip("-")


def _first_user_text(app: "FastAPI", sid: str) -> str:
    """Return the first user message's text for ``sid`` (empty when none is recorded yet)."""

    for msg in app.state.messages.get(sid, []) or []:
        if str(getattr(msg, "role", "") or "") != "user":
            continue
        text = " ".join(
            part.text.strip()
            for part in getattr(msg, "parts", []) or []
            if getattr(part, "type", "") == "text" and str(getattr(part, "text", "") or "").strip()
        ).strip()
        if text:
            return text
    return ""


def _compute_plan_file_path(app: "FastAPI", sid: str, session: Any) -> Path:
    """Compute the deterministic plan-file path for a session (P1.3 #1065).

    ``<plans>/<slug>.md`` where ``slug`` derives from the session's first user prompt (falling back
    to the session title, then the session id) and is suffixed with the session-id tail so two
    sessions sharing a first prompt never collide on one file. The path is asserted to fall within
    the P1.1 plan-ACL carve-out glob (``<plans>/*.md``) so the model can write it in plan mode.
    """

    from clio_agent.gact.runtime.grant_resolver import plans_dir  # noqa: PLC0415

    raw_sid = str(getattr(session, "id", "") or sid)
    sid_tail = _slugify(raw_sid.rsplit("_", 1)[-1]) or "session"
    base_slug = _slugify(_first_user_text(app, sid) or str(getattr(session, "title", "") or ""))
    slug = f"{base_slug}-{sid_tail}" if base_slug else sid_tail

    plans = plans_dir()
    path = (plans / f"{slug}.md").resolve(strict=False)
    plan_glob = f"{plans}{os.sep}*.md"
    assert fnmatch.fnmatchcase(str(path), plan_glob), (  # noqa: S101 - carve-out invariant guard
        f"computed plan-file path {path!r} escapes the plan-ACL carve-out {plan_glob!r}"
    )
    return path


def recorded_plan_file(session: Any) -> str | None:
    """Return the plan-file path recorded on ``session.metadata`` (``None`` when unset)."""

    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, Mapping):
        value = metadata.get(_PLAN_FILE_METADATA_KEY)
        if isinstance(value, str) and value:
            return value
    return None


def plan_file_exists(session: Any) -> bool:
    """Return whether the plan file recorded for ``session`` exists on disk (P1.3 #1065)."""

    path = recorded_plan_file(session)
    return path is not None and Path(path).exists()


def _plan_mode_reminder_block(*, full: bool, plan_file: str, exists: bool) -> str:
    """Compose the plan-mode reminder block (full contract or sparse one-liner).

    The FULL block carries the create-vs-edit branch (keyed on ``exists``), an adaptive-structure
    hint, the epistemic-ledger headers, the re-entry staleness note, the show-the-plan rule, the
    read-only restriction, and the turn-ending contract. The SPARSE block is a single line naming
    the restriction + the recorded plan path, so most turns cost almost nothing (do not bloat
    context). ``plan_file`` is the deterministic per-session path recorded on ``session.metadata``.
    """

    if not full:
        return (
            PLAN_MODE_REMINDER_MARKER
            + f" ({plan_file}). Keep writing your plan there; end your turn to hand it back "
            "for approval rather than executing it yourself."
        )
    if exists:
        create_or_edit = (
            f"A plan file already exists at {plan_file}. Make incremental edits to it as you learn."
        )
    else:
        create_or_edit = (
            f"No plan file exists yet. Create your plan at {plan_file} (write a *.md there — it is "
            "the ONLY writable path in plan mode)."
        )
    return (
        PLAN_MODE_REMINDER_MARKER + "\n\n"
        "You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, "
        "edit, and file-mutating tool is blocked.\n"
        f"- {create_or_edit}\n"
        "- Structure the plan to fit the task: Simple change → Changes + Verification; Standard "
        "task → Objective, Key Files & Context, Implementation Steps, Verification; Complex / "
        "architectural → Background, Scope, Proposed Solution, Alternatives, a phased Plan, "
        "Verification, Migration/Rollback.\n"
        "- Keep an epistemic ledger of what you know vs. must find out, under the headers: "
        "Given / Learned / To look up / To derive.\n"
        "- If a plan already exists, evaluate whether it is still relevant to THIS task before "
        "editing; treat a new task as a fresh plan.\n"
        "- Show the plan to the user in your response — don't just write it to disk.\n"
        "- Turn-ending contract: when the plan is complete, END YOUR TURN and hand it back for "
        "approval — do NOT try to execute the plan while in plan mode."
    )


def inject_plan_mode_reminder(app: "FastAPI", sid: str, session: Any, enriched_text: str) -> str:
    """Prepend the plan-mode reminder to this turn's input when the session is in plan mode.

    Returns ``enriched_text`` unchanged for every non-plan mode (edit/architect) — the
    attachment is scoped to ``plan`` for now (P1.2). In plan mode it prepends a reminder block
    and advances a tiny suppression counter on ``session.metadata`` so the FULL contract is
    injected on the first plan turn, immediately after any compaction, and once per
    :data:`_PLAN_REMINDER_FULL_INTERVAL` turns; a one-line marker is injected on every turn in
    between. Because it rides the per-turn input (not the system prompt), it survives compaction
    without invalidating the KV-cache prefix — the fix for "plan mode lost after compaction".
    """

    mode = str(getattr(session, "mode", "") or "")
    if mode != "plan":
        return enriched_text

    metadata = getattr(session, "metadata", None)
    state = metadata.get(_PLAN_REMINDER_STATE_KEY) if isinstance(metadata, Mapping) else None
    first_time = not isinstance(state, Mapping)

    # P1.3 #1065: compute the deterministic plan-file path ONCE and record it on session.metadata
    # (no fifth store). Thereafter the recorded path is re-read, so it is stable across turns.
    metadata_patch: dict[str, Any] = {}
    plan_file = recorded_plan_file(session)
    if plan_file is None:
        plan_file = str(_compute_plan_file_path(app, sid, session))
        metadata_patch[_PLAN_FILE_METADATA_KEY] = plan_file
    exists = Path(plan_file).exists()

    compactions = _session_compaction_count(app, sid)
    prev_turn = int(state.get("turn_index", 0)) if isinstance(state, Mapping) else 0
    last_full = int(state.get("last_full_turn", 0)) if isinstance(state, Mapping) else 0
    last_full_compactions = (
        int(state.get("compactions_at_last_full", 0)) if isinstance(state, Mapping) else 0
    )
    turn_index = prev_turn + 1

    compacted_since_full = compactions > last_full_compactions
    window_elapsed = (turn_index - last_full) >= _PLAN_REMINDER_FULL_INTERVAL
    full = first_time or compacted_since_full or window_elapsed

    metadata_patch[_PLAN_REMINDER_STATE_KEY] = {
        "turn_index": turn_index,
        "last_full_turn": turn_index if full else last_full,
        "compactions_at_last_full": compactions if full else last_full_compactions,
    }
    app.state.sessions.update(sid, metadata_patch=metadata_patch)
    return (
        _plan_mode_reminder_block(full=full, plan_file=plan_file, exists=exists)
        + "\n\n---\n\n"
        + enriched_text
    )


# =========================================================================== #
# P1.4 #1066 — plan_exit tool + N-way approval + constraint-lift + durable defer
# =========================================================================== #
#
# ``plan_exit`` is a TURN-ENDING YIELD, structurally identical to the ask-user pause
# (``turn_finalize.maybe_pause_for_user``): the model records the request via the tool, the
# post-forward seam (:func:`maybe_pause_for_plan_exit`) mints an approval ``UserQuestion`` and
# flips the session to ``waiting_user``, and the answer route (:func:`resolve_plan_exit_answer`)
# applies the approved mode transition + constraint-lifting message and resumes the run. Because
# the resume rides the SAME ``UserQuestion`` store + ``start_background_user_turn`` /
# ``enqueue_user_steer`` fold ask-user uses (#1031 deferred resume), an approval that arrives after
# the turn ends resumes as a new turn with no new store and no held thread (RULE 4 / ⚑).


class PlanExitError(RuntimeError):
    """A ``plan_exit`` precondition failed (not in plan mode, or no plan file exists).

    Raised by the ``plan_exit`` tool BEFORE any session mutation, so a rejected call leaves the
    session in plan mode unchanged (no silent fallback — the model sees a typed reason it can act
    on). ReAct surfaces the message as a tool observation the model reads and retries against.
    """


#: ``session.metadata`` key: a ``plan_exit`` the model requested this turn, awaiting the post-forward
#: seam to surface it as an approval question (no fifth store — rides the session record, #948 pattern).
_PLAN_EXIT_PENDING_KEY = "pending_plan_exit"

#: ``question.metadata`` flag marking a ``UserQuestion`` as a plan-exit N-way approval (P1.4 #1066).
#: The ask-user answer route branches on it to run :func:`resolve_plan_exit_answer` instead of the
#: generic ask-user resume.
PLAN_EXIT_APPROVAL_META = "plan_exit_approval"

#: The exit postures the model MAY hint via ``recommendedMode`` (the approver still has final say).
_PLAN_EXIT_RECOMMENDED_MODES = frozenset({"auto", "interactive", "exit_only"})

#: The approval decisions the approver selects. ``clear_context`` is a co-selectable MODIFIER, not a
#: decision — it may accompany any approve/reject.
_PLAN_EXIT_DECISIONS: tuple[str, ...] = ("auto", "interactive", "exit_only", "reject")
_PLAN_EXIT_CLEAR_CONTEXT = "clear_context"


def _record_plan_exit_request(
    app: "FastAPI",
    sid: str,
    session: Any,
    *,
    summary: str,
    recommended_mode: str,
    risk_notes: str,
) -> str:
    """Validate a ``plan_exit`` call and stash the pending request on ``session.metadata``.

    Hard-errors (raising :class:`PlanExitError`, mutating nothing) when the session is not in plan
    mode or no plan file exists at the recorded/expected path — the two required guardrails. On
    success the request rides ``session.metadata[_PLAN_EXIT_PENDING_KEY]`` (no fifth store) and the
    post-forward :func:`maybe_pause_for_plan_exit` seam surfaces it as an approval question.
    """

    mode = str(getattr(session, "mode", "") or "")
    if mode != "plan":
        raise PlanExitError(
            f"plan_exit is only available in plan mode (this session is in '{mode or 'edit'}' mode)."
        )
    plan_file = recorded_plan_file(session) or str(_compute_plan_file_path(app, sid, session))
    if not (plan_file and Path(plan_file).exists()):
        raise PlanExitError(
            f"cannot exit plan mode: no plan file exists at {plan_file}. Write your plan there "
            "(a *.md under the plans dir — the sole writable path in plan mode) before calling "
            "plan_exit."
        )
    clean_summary = str(summary or "").strip()
    if not clean_summary:
        raise PlanExitError(
            "plan_exit requires a 1-2 sentence 'summary' of the plan you are handing back for approval."
        )
    rec = str(recommended_mode or "").strip().lower()
    if rec and rec not in _PLAN_EXIT_RECOMMENDED_MODES:
        raise PlanExitError(
            f"recommendedMode must be one of {sorted(_PLAN_EXIT_RECOMMENDED_MODES)} "
            f"(got {recommended_mode!r})."
        )
    app.state.sessions.update(
        sid,
        metadata_patch={
            _PLAN_EXIT_PENDING_KEY: {
                "summary": clean_summary,
                "recommended_mode": rec,
                "risk_notes": str(risk_notes or "").strip(),
                "plan_file": plan_file,
                "surfaced": False,
            }
        },
    )
    return (
        f"Plan submitted for approval. Your plan at {plan_file} has been handed back to the user for "
        "an approve/reject decision. END YOUR TURN now — do NOT continue executing; you will be "
        "resumed with explicit authorization if the plan is approved."
    )


def build_plan_exit_tool(agent_def: Any) -> Any:
    """Build the ``plan_exit`` dspy.Tool — the model's turn-ending yield to request plan approval.

    Auto-attached to every react expert (like ``create_artifact``), it self-guards on plan mode so a
    call outside plan mode hard-errors ("not in plan mode") and a call with no plan file hard-errors
    naming the expected path. It reads the plan from ``session.metadata['plan_file']`` — there is NO
    plan-content parameter. On success it records the request and ends the turn for approval.
    """

    import dspy  # noqa: PLC0415

    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    def plan_exit(summary: str, recommendedMode: str = "", riskNotes: str = "") -> str:  # noqa: N803
        """Finish planning and hand the plan back to the user for approval to leave plan mode.

        Call this ONCE, at the end of planning, when your plan file is complete. It does NOT take
        the plan text — it reads the plan you wrote to the session's plan file. Provide a 1-2
        sentence ``summary``, optionally a ``recommendedMode`` (``auto`` = execute automatically,
        ``interactive`` = execute prompting per action, ``exit_only`` = leave plan mode but wait
        before executing), and optional ``riskNotes``. This ENDS YOUR TURN: the user approves or
        rejects out-of-band; on approval you are resumed with authorization to execute."""

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            raise PlanExitError("plan_exit requires an active CLIO app/session context.")
        session = app.state.sessions.get(sid)
        if session is None:
            raise PlanExitError("plan_exit could not resolve the active session.")
        return _record_plan_exit_request(
            app,
            sid,
            session,
            summary=summary,
            recommended_mode=recommendedMode,
            risk_notes=riskNotes,
        )

    return dspy.Tool(
        func=plan_exit,
        name="plan_exit",
        desc=plan_exit.__doc__,
        args={
            "summary": {
                "type": "string",
                "description": "1-2 sentence summary of the plan handed back for approval.",
            },
            "recommendedMode": {
                "type": "string",
                "description": "Optional exit posture hint: 'auto' | 'interactive' | 'exit_only'.",
            },
            "riskNotes": {
                "type": "string",
                "description": "Optional notes on risks/caveats the approver should weigh.",
            },
        },
    )


def _plan_exit_options() -> list[Any]:
    """Build the N-way approval options for a plan-exit question (P1.4 #1066)."""

    from clio_agent.gact.types import UserQuestionOption  # noqa: PLC0415

    return [
        UserQuestionOption(
            label="Approve — auto-execute",
            value="auto",
            description="Exit plan mode to an auto-accept posture and begin executing the plan.",
        ),
        UserQuestionOption(
            label="Approve — interactive",
            value="interactive",
            description="Exit plan mode; execute the plan but prompt before each action.",
        ),
        UserQuestionOption(
            label="Approve — exit only",
            value="exit_only",
            description="Leave plan mode but do NOT execute; await further direction.",
        ),
        UserQuestionOption(
            label="Reject — keep planning",
            value="reject",
            description="Stay in plan mode; return feedback so the plan can be revised.",
        ),
        UserQuestionOption(
            label="Also clear context (modifier)",
            value="clear_context",
            description="Modifier: clear the conversation history before resuming execution.",
        ),
    ]


def _plan_exit_prompt(summary: str, plan_file: str, recommended: str, risk_notes: str) -> str:
    """Compose the approval-question prompt shown to the user (P1.4 #1066)."""

    lines = [
        "The agent has finished planning and requests approval to leave plan mode.",
        f"Plan file: {plan_file}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    if recommended:
        lines.append(f"Recommended: {recommended}")
    if risk_notes:
        lines.append(f"Risk notes: {risk_notes}")
    lines.append(
        "\nApprove (auto / interactive / exit_only), optionally with 'clear_context', or reject "
        "with feedback."
    )
    return "\n".join(lines)


def maybe_pause_for_plan_exit(state: "TurnState") -> bool:
    """Surface a pending ``plan_exit`` as an approval question + yield the turn (P1.4 #1066).

    The post-forward seam, called from ``turn.py`` right after ``maybe_pause_for_user`` (the same
    architectural point, so the finalize region never clobbers the ``waiting_user`` status). When
    ``session.metadata[_PLAN_EXIT_PENDING_KEY]`` holds an un-surfaced request and the turn has not
    errored, it mints the N-way approval :class:`~clio_agent.gact.types.UserQuestion`, flips the
    session to ``waiting_user``, finalizes the context frame, settles the transcript ledger, and
    returns ``True`` — the orchestrator then returns before the finalize region (exactly like the
    ask-user pause). Returns ``False`` (turn proceeds normally) when there is nothing to surface.
    """

    if getattr(state, "error_info", None) is not None:
        return False
    app = state.app
    session = app.state.sessions.get(state.sid)
    if session is None:
        return False
    metadata = getattr(session, "metadata", None)
    pending = metadata.get(_PLAN_EXIT_PENDING_KEY) if isinstance(metadata, Mapping) else None
    # An empty dict is the resolved tombstone `resolve_plan_exit_answer` writes (a shallow
    # `sessions.update` merge cannot delete the key). Treat `{}` as ABSENT — matching how
    # `_get_loop`/`_get_goal` read `{}` — so a resumed turn never re-surfaces a phantom second
    # approval. `_record_plan_exit_request` always writes a non-empty dict, so this is unambiguous.
    if not isinstance(pending, Mapping) or not pending or pending.get("surfaced"):
        return False

    from clio_agent.gact.enrichment import _finalize_context_frame  # noqa: PLC0415
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _emit_semantic_event,
        _new_question_id,
    )
    from clio_agent.gact.turn_stream import settle_turn_transcript  # noqa: PLC0415
    from clio_agent.gact.types import UserQuestion  # noqa: PLC0415

    plan_file = str(pending.get("plan_file") or recorded_plan_file(session) or "")
    summary = str(pending.get("summary") or "")
    recommended = str(pending.get("recommended_mode") or "")
    risk_notes = str(pending.get("risk_notes") or "")
    now_iso = datetime.now(timezone.utc).isoformat()
    question = UserQuestion(
        id=_new_question_id(),
        session_id=state.sid,
        prompt=_plan_exit_prompt(summary, plan_file, recommended, risk_notes),
        status="pending",
        kind="choice",
        options=_plan_exit_options(),
        created_at=now_iso,
        updated_at=now_iso,
        source="plan_exit",
        turn_id=state.user_msg.id,
        attempt_id=getattr(state, "retry_attempt_id", "") or "",
        metadata={
            PLAN_EXIT_APPROVAL_META: True,
            "resume_on_answer": True,
            "recommended_mode": recommended,
            "summary": summary,
            "risk_notes": risk_notes,
            "plan_file": plan_file,
            "source_user_message_id": state.user_msg.id,
        },
    )
    app.state.user_questions[question.id] = question
    updated = app.state.sessions.update(
        state.sid,
        status="waiting_user",
        message_count=len(app.state.messages.get(state.sid, [])),
        metadata_patch={
            "pending_user_question_id": question.id,
            _PLAN_EXIT_PENDING_KEY: {**dict(pending), "surfaced": True, "question_id": question.id},
        },
    )
    _finalize_context_frame(
        app, state.sid, state.context_frame["id"], "", "completed", error_info=None
    )
    _emit_semantic_event(
        app,
        state.sid,
        "user_question.created",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="waiting_user",
        summary="Agent requested approval to exit plan mode.",
        actor={"agent_id": state.selected_agent or state.invocation_agent_id},
        subject={"question_id": question.id},
        payload=question.model_dump(exclude_none=True),
    )
    state.bus.publish(
        Event(
            type="user_question.created",
            session_id=state.sid,
            payload=question.model_dump(exclude_none=True),
        )
    )
    state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=state.sid,
            payload={
                "session_id": state.sid,
                "status": "waiting_user",
                "prev_status": "running",
                "updated_at": updated.updated_at if updated is not None else "",
                "pending_user_question_id": question.id,
            },
        )
    )
    settle_turn_transcript(state)
    return True


#: The Gemini "State Transition Override" constraint-lifting preamble injected into the resumed
#: turn on approval — the explicit signal that plan mode's read-only restrictions are lifted.
_CONSTRAINT_LIFT_HEADER = "[STATE TRANSITION OVERRIDE]"


def _plan_exit_constraint_lift_text(decision: str, plan_file: str) -> str:
    """Compose the constraint-lifting resume text for an APPROVED plan exit (auto/interactive).

    Names the state transition explicitly (previous read-only/plan constraints are lifted; the model
    is authorized to modify files to implement the approved plan) and points at the plan file. The
    ``auto`` variant tells the model to begin executing now; the ``interactive`` variant tells it to
    expect a prompt per action. Never used for ``exit_only`` (which injects NO execute-now message).
    """

    base = (
        f"{_CONSTRAINT_LIFT_HEADER} Your plan at {plan_file} has been APPROVED. The previous "
        "read-only / plan-mode constraints are now LIFTED — you are authorized to modify files to "
        "implement the approved plan."
    )
    if decision == "interactive":
        return base + " Begin implementing it; you will be prompted to approve each action."
    return base + " Begin implementing the approved plan now."


def _plan_exit_reject_text(feedback: str, plan_file: str) -> str:
    """Compose the resume text for a REJECTED plan exit (stays in plan mode with feedback)."""

    note = feedback or "(no additional feedback provided)"
    return (
        "Your request to exit plan mode was REJECTED — you are STILL in plan mode. Revise the plan "
        f"at {plan_file} per the reviewer's feedback, then call plan_exit again.\n\n"
        f"Reviewer feedback: {note}"
    )


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
    """Resume the run after a plan-exit decision, riding the #1031 deferred-resume fold.

    Mirrors the ask-user answer staging: if a turn is in flight the resume is folded into the loop
    inbox as a user steer (drained mid-turn or re-driven into one new turn by the idle hook); if the
    session is idle/waiting_user (the durable-defer case — approval arrived after the turn ended) it
    stages a background user turn immediately. No held thread, no new store.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.loop_inbox import enqueue_user_steer  # noqa: PLC0415

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


def resolve_plan_exit_answer(app: "FastAPI", deps: "GactDeps", sid: str, question: Any) -> None:
    """Apply an answered plan-exit approval: mode transition + constraint-lift + resume (P1.4 #1066).

    Called from the ask-user answer route when the answered question carries
    :data:`PLAN_EXIT_APPROVAL_META`. Parses the decision (``auto``/``interactive``/``exit_only``/
    ``reject``) and the ``clear_context`` modifier from the answer, then:

    * **reject** — leaves ``session.mode`` == ``plan`` and resumes with the reviewer's feedback so
      the model can revise the plan.
    * **auto / interactive** — transitions ``session.mode`` to ``edit`` (approval_mode ``auto-edits``
      for auto, ``ask`` for interactive), optionally clears history, and resumes with the
      constraint-lifting message.
    * **exit_only** — transitions ``session.mode`` to ``edit`` but does NOT resume a turn and injects
      NO execute-now message; the model must wait for the user's next direction before editing.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    session = app.state.sessions.get(sid)
    q_meta = getattr(question, "metadata", None) or {}
    selected = [str(s) for s in (getattr(question, "selected_options", None) or [])]
    answer_meta = getattr(question, "answer_metadata", None) or {}
    plan_file = str(q_meta.get("plan_file") or "")
    decision = next((s for s in selected if s in _PLAN_EXIT_DECISIONS), "")
    if not decision:
        # No explicit human decision selected: reject-safe unconditionally (stay in plan mode —
        # NEVER silently execute an unapproved plan). The model's recommended_mode may pre-select
        # or hint the option in the UI, but it MUST NOT substitute for the human's decision here;
        # the approver still has final say (see the invariant at the top of this module).
        decision = "reject"
    clear_context = (_PLAN_EXIT_CLEAR_CONTEXT in selected) or bool(answer_meta.get("clear_context"))
    feedback = str(getattr(question, "answer", "") or "").strip()

    # Clear the surfaced-request bookkeeping first (the decision is now being applied).
    app.state.sessions.update(
        sid, metadata_patch={_PLAN_EXIT_PENDING_KEY: {}, "pending_user_question_id": ""}
    )

    if decision == "reject":
        session = app.state.sessions.get(sid)
        _stage_plan_exit_resume(
            app,
            deps,
            sid,
            session,
            _plan_exit_reject_text(feedback, plan_file),
            {"plan_exit_result": "rejected", "plan_file": plan_file},
            question_id=question.id,
        )
        return

    # Approve: the SANCTIONED plan-mode exit (unlike the enter_mode no-escape guard).
    approval_mode = "auto-edits" if decision == "auto" else "ask"
    app.state.sessions.update(sid, mode="edit", approval_mode=approval_mode)
    cleared = False
    if clear_context:
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
    }

    if decision == "exit_only":
        # Leave plan mode but do NOT execute: no resume turn, no execute-now message.
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
        app.state.bus.publish(
            Event(
                type="plan_exit.resolved",
                session_id=sid,
                payload={
                    "decision": "exit_only",
                    "cleared_context": cleared,
                    "plan_file": plan_file,
                },
            )
        )
        return

    _stage_plan_exit_resume(
        app,
        deps,
        sid,
        session,
        _plan_exit_constraint_lift_text(decision, plan_file),
        resume_metadata,
        question_id=question.id,
    )
