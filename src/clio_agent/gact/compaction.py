"""Compaction: ONE operation, two triggers, visible and lossless.

clio-core holds everything. A compaction changes only the MODEL's context view of one
agent scope: the live segments :func:`~clio_agent.gact.compaction_policy.
post_compaction_context` picks are replaced (``summarize_segments``, a recorded op) by
one summary that renders first, ahead of what the policy keeps verbatim (by default the
current user question). Every replaced step stays in clio-core, searchable and
recallable byte-exact (``recall_context``).

Both triggers run :func:`compact_session_context`: ``POST /v1/sessions/{sid}/compact``
(``trigger="manual"``; the context panel passes ``?scope=``) and the threshold check
the loop runs between ReAct steps (:func:`maybe_autocompact`, ``trigger="auto"``).
An auto compaction that leaves the context over the threshold is not repeated in the
same forward: the next ones are a typed skip with one ``notice``
(:class:`AutoCompactionGuard`).

Per compacted scope:

1. ``compaction.started`` (highway semantic event, served to the UI);
2. the summary (one LM call over what is replaced);
3. the record, written durably where it happened (:mod:`clio_agent.gact.
   compaction_record`): mid-turn a summarization injection in the open turn's
   assistant message at this step boundary, between turns its own row;
4. the fold. A record that cannot be written folds nothing; a fold that fails
   retracts the record;
5. ``compaction.completed`` -- or ``compaction.failed`` for any failure after
   ``started`` (always paired), then the typed :class:`CompactionError`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from clio_agent.arc import history_mode
from clio_agent.errors import ClioError
from clio_agent.gact import context as _ctx
from clio_agent.gact.compaction_policy import keep_policy, post_compaction_context
from clio_agent.gact.conversation_projection import model_context_messages
from clio_agent.gact.delegation import _compact_exact_evidence_index
from clio_agent.gact.runtime.context_tokens import _estimate_text_tokens
from clio_agent.gact.runtime.globals import _active_semantic_turn_id, _emit_semantic_event
from clio_agent.gact.summarization_record import (
    failure_notice_part,
    recall_line,
    skipped_notice_part,
    summarization_part,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime.stream_audit import stream_audit

__all__ = [
    "AUDIT_AUTO_FAILED",
    "AUDIT_AUTO_SKIPPED",
    "SKIP_ABOVE_THRESHOLD_AFTER_COMPACTION",
    "SKIP_NO_LIVE_CONTEXT",
    "SKIP_NOTHING_NEW",
    "AutoCompactionFailedError",
    "AutoCompactionGuard",
    "CompactionError",
    "compact_session_context",
    "maybe_autocompact",
]

#: Skip reasons: ``compact_session_context`` returns ``{"compacted": False, "reason": ...}``.
SKIP_NO_LIVE_CONTEXT = "no_live_context"
SKIP_NOTHING_NEW = "nothing_new_since_last_compaction"
SKIP_NO_TOKEN_COUNT = "no_token_count"
#: Auto only: the last auto compaction of this forward left the context over the
#: threshold, so another one would summarize little more than the summary again.
SKIP_ABOVE_THRESHOLD_AFTER_COMPACTION = "above_threshold_after_compaction"

#: The plain-language ``notice`` of that skip, written once per forward.
ABOVE_THRESHOLD_NOTICE = (
    "Context is still near the limit after summarizing; continuing without "
    "summarizing again this turn."
)

#: Audit reasons (``stream_audit`` stage names double as the reason).
AUDIT_AUTO_FAILED = "compaction.auto_failed"
AUDIT_AUTO_SKIPPED = "compaction.auto_skipped"

_BOUNDED_CHARS = 300


class CompactionError(Exception):
    """One typed error surface for both triggers.

    The manual route turns it into an HTTP response (``HTTPException(exc.status,
    exc.envelope())``); the auto trigger fails the turn with
    :class:`AutoCompactionFailedError`.
    """

    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.details = details or {}
        self.recoverable = recoverable

    def envelope(self) -> dict[str, Any]:
        """The HTTP body: an :class:`ErrorEnvelope`, same shape every route uses."""
        return ErrorEnvelope(
            error=ErrorInfo(
                error=self.error,
                message=self.message,
                details=self.details,
                recoverable=self.recoverable,
            )
        ).model_dump(exclude_none=True)


class AutoCompactionFailedError(ClioError):
    """The proactive compaction a turn needed failed: the turn fails typed, nothing folded."""

    reason = "auto_compaction_failed"

    def __init__(self, exc: CompactionError, session_id: str) -> None:
        super().__init__(
            f"automatic context compaction failed ({exc.error}): {exc.message}",
            error_type=self.reason,
            details={"session_id": session_id, "compaction_error": exc.error},
        )


def _skip(sid: str, reason: str) -> dict[str, Any]:
    return {"session_id": sid, "compacted": False, "reason": reason, "compactions": []}


# ---------------------------------------------------------------------------
# The summarizer prompt's inputs (the template: ``clio_agent.compaction_prompt``).
# ---------------------------------------------------------------------------


def _bounded(text: str, limit: int = _BOUNDED_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _context_file_inventory(app: Any, sid: str) -> str:
    """Render stable facts for files attached outside the message ledger."""
    registry = getattr(app.state, "context_files", {}) or {}
    bucket = registry.get(sid, {}) or {}
    rows: list[str] = []
    for raw in bucket.values():
        row = raw if isinstance(raw, Mapping) else {}
        path = str(row.get("display_path") or row.get("path") or "").strip()
        if not path:
            continue
        facts = [f"path={_bounded(path)}"]
        for key in ("mode", "size", "language"):
            value = row.get(key)
            if value not in (None, ""):
                facts.append(f"{key}={_bounded(str(value))}")
        rows.append("- " + "; ".join(facts))
    return "\n".join(sorted(rows))


def _message_lines(message: Any) -> list[str]:
    role = str(message.role).upper()
    lines: list[str] = []
    for part in message.parts:
        kind = type(part).__name__
        if kind in ("TextPart", "ThinkingPart") and part.text.strip():
            lines.append(f"{role}: {part.text.strip()}")
        elif kind == "ToolCallPart":
            lines.append(f"{role}: tool call: {part.name}({_bounded(str(dict(part.input)))})")
        elif kind == "ToolResultPart":
            status = "error" if part.is_error else "ok"
            text = "".join(getattr(p, "text", "") for p in part.content)
            lines.append(f"TOOL: tool result: {part.name} {status}: {_bounded(text)}")
        elif kind in ("ImagePart", "DocumentPart"):
            lines.append(f"{role}: [attached {kind.removesuffix('Part').lower()}]")
    return lines


# ---------------------------------------------------------------------------
# What is compacted.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScopePlan:
    """One scope's compaction, decided before anything is written."""

    scope: str
    summarize: tuple[str, ...]
    kept_turns: frozenset[str]
    transcript: str


def _open_turn_id(app: Any, sid: str) -> str:
    from clio_agent.gact.tool_observer import _session_turn_transcript  # noqa: PLC0415

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None and not transcript.frozen:
        return str(transcript.turn_id or "")
    return _active_semantic_turn_id()


def _scopes(app: Any, sid: str, scope: str) -> list[str]:
    """The scope asked for; inside a turn the running one; else every agent scope."""
    from clio_agent.gact.agents.clio_react_record import arc_scope  # noqa: PLC0415

    if scope:
        return [scope]
    active, session, running = arc_scope()
    if active is not None and session == sid:
        return [running]
    return sorted(s for s in app.state.arc.list_segment_scopes(sid) if not s.startswith("_"))


def _plan(app: Any, sid: str, scope: str, open_turn: str) -> _ScopePlan | None:
    """What the policy summarizes in ``scope`` (``None``: nothing to compact)."""
    from clio_agent.gact.agents.clio_react_record import (  # noqa: PLC0415
        ContextFoldError,
        fold_steps,
    )

    live = list(app.state.arc.render_working_set(sid, scope))
    decision = post_compaction_context(live, open_turn_id=open_turn, policy=keep_policy())
    replaced = [s for s in live if s.id in set(decision.summarize)]
    if not replaced or (len(replaced) == 1 and replaced[0].kind == "summary"):
        return None
    try:
        lines = [line for m in fold_steps(replaced) for line in _message_lines(m)]
    except ContextFoldError as exc:
        raise CompactionError(409, exc.reason, str(exc), dict(exc.details or {})) from exc
    if not lines:
        return None
    kept = frozenset(s.turn_id for s in live if s.id in set(decision.keep))
    return _ScopePlan(scope, decision.summarize, kept, "\n".join(lines))


def _covered_rows(app: Any, sid: str, open_turn: str, kept_turns: frozenset[str]) -> list[str]:
    """The transcript rows the summary stands in for in a summarizer prompt."""
    rows = model_context_messages(list(app.state.messages.get(sid, [])))
    skip = set(kept_turns) | {open_turn}
    return [m.id for m in rows if (getattr(m, "turn_id", "") or m.id) not in skip]


# ---------------------------------------------------------------------------
# The one operation.
# ---------------------------------------------------------------------------


def compact_session_context(
    app: Any,
    sid: str,
    *,
    trigger: Literal["manual", "auto"],
    focus: str = "",
    scope: str = "",
) -> dict[str, Any]:
    """Compact ``sid``'s agent context. Blocking, off-loop only (store RPCs, LM calls).

    Args:
        app: The FastAPI app.
        sid: The session being compacted.
        trigger: ``"manual"`` (``POST /compact``) or ``"auto"`` (the threshold).
        focus: Optional user-supplied focus instructions (manual only).
        scope: The agent scope to compact; ``""`` means the running scope inside a
            turn, else every agent scope of the session (one compaction each).

    Returns:
        ``{session_id, compacted, compactions: [...]}``, one entry per compacted scope
        (``compaction_id``, ``scope``, ``trigger``, ``turn_id``, ``message_id``,
        ``part_id``, ``replaced_count``, ``summary``); ``reason`` on a typed skip.

    Raises:
        CompactionError: session not found (404), History mode (409), no clio-core
            context (503), a fold-unreadable context (409), no LM agent (503), the LM
            call failed (502) or the record / fold could not be written (500).
    """
    if app.state.sessions.get(sid) is None:
        raise CompactionError(404, "not_found", f"session not found: {sid}", {"session_id": sid})
    if history_mode.active():
        raise CompactionError(
            409,
            history_mode.HistoryModeUnsupportedError.reason,
            "compaction needs clio-core; this CLIO runs in History mode",
            {"session_id": sid, "context_mode": "history"},
        )
    if getattr(app.state, "arc", None) is None:
        raise CompactionError(
            503, "context_store_unavailable", "no clio-core context to compact", {"session_id": sid}
        )
    open_turn = _open_turn_id(app, sid)
    scopes = _scopes(app, sid, scope)
    if not any(app.state.arc.render_working_set(sid, s) for s in scopes):
        return _skip(sid, SKIP_NO_LIVE_CONTEXT)
    plans = [p for p in (_plan(app, sid, s, open_turn) for s in scopes) if p is not None]
    if not plans:
        return _skip(sid, SKIP_NOTHING_NEW)
    done = [_compact_scope(app, sid, plan, trigger, focus, open_turn) for plan in plans]
    return {"session_id": sid, "compacted": True, "compactions": done}


def _event(app: Any, sid: str, kind: str, turn_id: str, payload: dict[str, Any]) -> None:
    status = {"started": "running", "completed": "completed", "failed": "failed"}[kind]
    _emit_semantic_event(
        app,
        sid,
        f"compaction.{kind}",
        turn_id=turn_id,
        status=status,
        summary=f"Context compaction {kind}.",
        actor={"role": "runtime", "component": "compaction"},
        subject={"compaction_id": payload["compaction_id"], "scope": payload["scope"]},
        payload=payload,
    )


def _compact_scope(
    app: Any, sid: str, plan: _ScopePlan, trigger: str, focus: str, open_turn: str
) -> dict[str, Any]:
    """Compact one scope: started, summary, record, fold, completed (or failed)."""
    base = {
        "session_id": sid,
        "compaction_id": f"cmp_{uuid.uuid4().hex[:16]}",
        "scope": plan.scope,
        "trigger": trigger,
        "turn_id": open_turn,
    }
    _event(app, sid, "started", open_turn, base)
    try:
        result = _summarize_record_fold(app, sid, plan, trigger, focus, base)
    except CompactionError as exc:
        _failed(app, sid, base, exc)
        raise
    except Exception as exc:  # noqa: BLE001 - reported failed and re-raised typed
        typed = CompactionError(500, "compaction_failed", f"compaction failed: {exc!r}")
        _failed(app, sid, base, typed)
        raise typed from exc
    _event(app, sid, "completed", open_turn, {k: v for k, v in result.items() if k != "summary"})
    return result


def _failed(app: Any, sid: str, base: dict[str, Any], exc: CompactionError) -> None:
    """Record the failure where it happened (a ``notice`` the model is never told),
    then ``compaction.failed`` naming it. A notice that cannot be written is reported
    in the same event and raised typed with both causes."""
    from clio_agent.gact.compaction_record import write_notice  # noqa: PLC0415

    notice = failure_notice_part(
        f"Summarizing the context failed, so it was left as it was. {exc.message}",
        code=exc.error,
        compaction_id=base["compaction_id"],
        trigger=base["trigger"],
        agent_id=base["scope"],
    )
    error = {"code": exc.error, "message": exc.message}
    try:
        message_id, part_id = write_notice(app, sid, notice)
    except Exception as notice_exc:  # noqa: BLE001 - reported failed, raised typed below
        _event(app, sid, "failed", base["turn_id"], {**base, "error": error, "part_id": ""})
        raise CompactionError(
            500,
            "compaction_failure_unrecorded",
            f"{exc.message}; its failure notice could not be written: {notice_exc!r}",
            {"compaction_error": exc.error},
            recoverable=False,
        ) from notice_exc
    payload = {**base, "error": error, "message_id": message_id, "part_id": part_id}
    _event(app, sid, "failed", base["turn_id"], payload)


def _summarize_record_fold(
    app: Any, sid: str, plan: _ScopePlan, trigger: str, focus: str, base: dict[str, Any]
) -> dict[str, Any]:
    from clio_agent.gact.compaction_record import RecordWriteError, write_record  # noqa: PLC0415
    from clio_agent.gact.hooks import dispatch_pre_compact  # noqa: PLC0415

    sess = app.state.sessions.get(sid)
    dispatch_pre_compact(
        session_id=sid,
        cwd=str(getattr(sess, "workspace_root", "") or ""),
        payload={
            "message_count": len(plan.summarize),
            "transcript_chars": len(plan.transcript),
            "trigger": trigger,
        },
    )
    summary = _summary(app, sid, plan.transcript, focus)
    text = f"{summary}\n\n{recall_line(base['compaction_id'])}"
    part = summarization_part(
        text,
        trigger=trigger,
        compaction_id=base["compaction_id"],
        derived_from=plan.summarize,
        compacted_message_ids=_covered_rows(app, sid, base["turn_id"], plan.kept_turns),
        agent_id=plan.scope,
    )
    try:
        record = write_record(app, sid, part)
    except RecordWriteError as exc:
        raise CompactionError(500, exc.reason, str(exc), dict(exc.details or {})) from exc
    _fold(app, sid, plan, text, base, record)
    message_id, part_id = record.publish()
    _remember(app, sid, base, message_id, len(plan.summarize))
    return {
        **base,
        "message_id": message_id,
        "part_id": part_id,
        "replaced_count": len(plan.summarize),
        "summary": text,
    }


def _summary(app: Any, sid: str, transcript: str, focus: str) -> str:
    agent = getattr(app.state, "agent", None)
    if agent is None:
        raise CompactionError(
            503, "agent_unavailable", "no LM agent wired; configure one via PUT /v1/providers/lm"
        )
    from clio_agent.compaction_prompt import (  # noqa: PLC0415
        CompactionPromptError,
        render_compaction_prompt,
    )

    try:  # read from the configured file at every compaction (no restart, no fallback)
        prompt = render_compaction_prompt(
            transcript, focus=focus, files=_context_file_inventory(app, sid)
        )
    except CompactionPromptError as exc:
        raise CompactionError(500, exc.reason, str(exc), dict(exc.details or {})) from exc

    def _call() -> str:
        return str(agent._run_chat_agent(prompt, "") or "")

    retry_call = getattr(agent, "_call_with_transient_provider_retries", None)
    try:
        summary = retry_call("compact_summary", _call) if callable(retry_call) else _call()
    except Exception as exc:  # noqa: BLE001 - re-raised typed, never silently dropped
        raise CompactionError(
            502, "upstream_error", f"compact summarisation failed: {exc!r}"
        ) from exc
    if not summary.strip():
        raise CompactionError(
            502, "empty_summary", "the summary LM returned no text; nothing was compacted"
        )
    evidence_index = _compact_exact_evidence_index(transcript)
    return summary.strip() + (f"\n\n{evidence_index}" if evidence_index else "")


def _fold(
    app: Any, sid: str, plan: _ScopePlan, text: str, base: dict[str, Any], record: Any
) -> None:
    """Replace the planned ids with the summary, ahead of what is kept. A failed fold
    retracts the (already durable) record before it raises typed."""
    try:
        app.state.arc.summarize_segments(
            sid,
            plan.scope,
            list(plan.summarize),
            {"text": text, "compaction_id": base["compaction_id"]},
            token_count=_estimate_text_tokens(text),
            turn_id=base["turn_id"] or record.message_id,
            position=0,
        )
    except Exception as exc:  # noqa: BLE001 - the record is retracted, then raised typed
        _retract(record, exc)
        raise CompactionError(
            500, "fold_failed", f"the context fold failed: {exc!r}", {"scope": plan.scope}
        ) from exc
    from clio_agent.providers.stateful_common import (  # noqa: PLC0415
        note_prefix_reset_for_active_scope,
    )

    note_prefix_reset_for_active_scope("ops_reset")


def _retract(record: Any, cause: BaseException) -> None:
    try:
        record.retract()
    except Exception as exc:  # noqa: BLE001 - both failures raised typed together
        raise CompactionError(
            500,
            "record_retract_failed",
            f"the fold failed ({cause!r}) and its record could not be retracted: {exc!r}",
            {"part_id": record.part_id},
            recoverable=False,
        ) from exc


def _remember(app: Any, sid: str, base: dict[str, Any], message_id: str, replaced: int) -> None:
    """The session's memory-event row (``GET /v1/sessions/{sid}/memory_events``)."""
    now = datetime.now(timezone.utc).isoformat()
    app.state.memory_events.setdefault(sid, []).append(
        {
            "id": base["compaction_id"],
            "version": 1,
            "type": "compact_summary",
            "session_id": sid,
            "created_at": now,
            "updated_at": now,
            "summary_message_id": message_id,
            "archived_count": replaced,
            "trigger": base["trigger"],
            "scope": base["scope"],
        }
    )


# ---------------------------------------------------------------------------
# Auto trigger.
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactionGuard:
    """One ReAct forward's auto-compaction state (the loop owns one per forward).

    After an auto compaction the next step's model call measures what is left (system
    prompt, tools, summary, the question kept verbatim). When that is still over the
    threshold, compacting again cannot get under it: every later step would summarize
    the summary and restart a stateful provider's session. So the guard stops auto
    compaction for the rest of the forward; manual compaction is unaffected.

    Attributes:
        compaction_id: The last auto compaction of this forward (``""``: none yet).
        awaiting_measurement: That compaction's result is not measured yet (the next
            step boundary reads the first model call made after it).
        above_threshold: The measured context stayed over the threshold after it; the
            forward does not auto-compact again.
    """

    compaction_id: str = ""
    awaiting_measurement: bool = False
    above_threshold: bool = False


def maybe_autocompact(guard: AutoCompactionGuard) -> None:
    """Compact the running scope between ReAct steps when its context is too full.

    The loop calls this at every step boundary with its forward's ``guard``. When the
    last measured prompt size over the context window crosses the session's
    threshold, the running scope is compacted with ``trigger="auto"`` (the same
    operation as a manual compact). A failed compaction is NOT swallowed: it is
    audited (:data:`AUDIT_AUTO_FAILED`) and raised as
    :class:`AutoCompactionFailedError`, so the turn fails typed with its context
    unfolded. With no measured count yet in this binding, the previous turn's durable
    per-scope usage stands in (subscription providers bind a fresh LM per turn).
    History mode has no compaction; no active app is an audited skip. When the first
    measurement after an auto compaction is still over the threshold, every later
    crossing in the forward is an audited skip
    (:data:`SKIP_ABOVE_THRESHOLD_AFTER_COMPACTION`) and the first one writes a
    ``notice`` saying so.
    """
    from clio_agent.gact.agents.clio_react_record import arc_scope  # noqa: PLC0415
    from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
        _last_prompt_tokens,
        _session_autocompact_preferences,
    )

    arc, session, scope = arc_scope()
    if arc is None or history_mode.active():
        return  # History mode has no compaction (declared on the health row and the UI)
    app = _ctx.active_app()
    if app is None:
        stream_audit(AUDIT_AUTO_SKIPPED, reason="no_active_app", session_id=session)
        return
    sessions = getattr(app.state, "sessions", None)  # a bare loop app has no session store
    metadata = getattr(sessions.get(session) if sessions is not None else None, "metadata", None)
    enabled, threshold = _session_autocompact_preferences(metadata)
    if not enabled:
        return
    window = _ctx.active_react_context_window()
    last = _last_prompt_tokens() or _durable_prompt_tokens(metadata, scope)
    if not window or not last:
        stream_audit(AUDIT_AUTO_SKIPPED, reason=SKIP_NO_TOKEN_COUNT, session_id=session)
        return
    over = (last / window) >= threshold
    if guard.awaiting_measurement:
        guard.awaiting_measurement = False
        guard.above_threshold = over
        if over:
            _notice_above_threshold(app, session, scope, guard.compaction_id)
    if not over:
        return
    if guard.above_threshold:
        stream_audit(
            AUDIT_AUTO_SKIPPED,
            reason=SKIP_ABOVE_THRESHOLD_AFTER_COMPACTION,
            session_id=session,
            scope=scope,
            prompt_tokens=last,
            context_window=window,
            threshold=threshold,
            compaction_id=guard.compaction_id,
        )
        return
    try:
        result = compact_session_context(app, session, trigger="auto", scope=scope)
    except CompactionError as exc:
        stream_audit(AUDIT_AUTO_FAILED, session_id=session, error=exc.error, message=exc.message)
        raise AutoCompactionFailedError(exc, session) from exc
    if result["compacted"]:
        guard.compaction_id = str(result["compactions"][-1]["compaction_id"])
        guard.awaiting_measurement = True


def _notice_above_threshold(app: Any, sid: str, scope: str, compaction_id: str) -> None:
    """Record, once per forward, that auto compaction stops (a ``notice`` the model is
    never told). A notice that cannot be written fails the turn typed."""
    from clio_agent.gact.compaction_record import write_notice  # noqa: PLC0415

    notice = skipped_notice_part(
        ABOVE_THRESHOLD_NOTICE,
        code=SKIP_ABOVE_THRESHOLD_AFTER_COMPACTION,
        compaction_id=compaction_id,
        agent_id=scope,
    )
    try:
        write_notice(app, sid, notice)
    except Exception as exc:  # noqa: BLE001 - raised typed: the skip must be visible
        error = CompactionError(
            500,
            "compaction_skip_unrecorded",
            f"auto compaction stopped but its notice could not be written: {exc!r}",
            {"compaction_id": compaction_id, "scope": scope},
            recoverable=False,
        )
        stream_audit(AUDIT_AUTO_FAILED, session_id=sid, error=error.error, message=error.message)
        raise AutoCompactionFailedError(error, sid) from exc


def _durable_prompt_tokens(metadata: Any, scope: str) -> int:
    """The previous turn's measured prompt tokens for ``scope`` (0 when not measured)."""
    by_scope = metadata.get("context_usage_by_scope", {}) if isinstance(metadata, Mapping) else {}
    usage = by_scope.get(scope.partition("#run")[0], {}) if isinstance(by_scope, Mapping) else {}
    if not isinstance(usage, Mapping) or usage.get("source") == "estimated":
        return 0  # an ``estimated`` usage is the user prompt alone, not a measurement
    value = usage.get("used_tokens", 0)
    return int(value) if isinstance(value, int | float) else 0
