"""#1339: compaction is ONE operation -- an appended checkpoint -- with two triggers.

Both ``POST /v1/sessions/{sid}/compact`` (manual) and the proactive threshold-crossing
autocompact (auto) now go through :func:`compact_session_context`. Neither destroys
the ledger: a checkpoint is an ordinary assistant message carrying one ``compaction``
part, APPENDED after the rows it stands in for -- history is retained in full for
display and undo; only the MODEL-facing prompt shrinks at the checkpoint (see
:mod:`clio_agent.gact.conversation_projection`).

Deleted along with this: ``gact/compact_memory.py`` (the ARC ``conversations``-record
mirror -- its readers were unreachable) and the ``session_archives`` ledger snapshot
(zero readers). The old ``ledger[-50:]`` summariser cap is gone too: the checkpoint IS
the bound now, by construction, so there is nothing left to truncate defensively.

ARC's live working set has meaning only INSIDE a turn (the per-turn reset in
``reactv2_events.instrumented_forward``); a manual compact between turns has no scope
to fold, so ``arc_status`` is a typed description of that reality
(:data:`ARC_STATUSES`), never a fabricated "stored". A MANUAL compact issued WHILE a
turn is running (``trigger="manual"`` during an open minter, see Placement below)
still reports :data:`ARC_NO_ACTIVE_SCOPE`: it executes on the route's off-loop
executor thread, whose contextvars carry no react scope even though one is live
inside that turn's own forward call -- only the AUTO trigger, which runs from inside
``instrumented_forward`` itself, ever observes a scope to fold.

Placement: a checkpoint is never inserted ahead of an in-flight assistant message (that
would reorder ``reload`` ahead of ``live``, see design note in the tracking issue), so
while a turn's transcript minter is open the checkpoint is STAGED
(:func:`stage_checkpoint`) and flushed right after that turn's assistant message
persists (:func:`flush_staged_checkpoint`, wired from
``part_atom_minter.persist_finalized_message`` / ``close_turn_minter``). Otherwise it
is appended immediately (:func:`append_checkpoint`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.conversation_projection import model_context_messages
from clio_agent.gact.delegation import _compact_exact_evidence_index
from clio_agent.gact.events import Event
from clio_agent.gact.routes.compaction import build_compact_summary_message
from clio_agent.gact.runtime.context_tokens import (
    _estimate_text_tokens,
    _resolve_expert_context_window,
)
from clio_agent.gact.runtime.globals import (
    _active_semantic_turn_id,
    _emit_semantic_event,
    _new_memory_event_id,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Message
from clio_agent.runtime.stream_audit import stream_audit

__all__ = [
    "ARC_STATUSES",
    "ARC_FOLDED",
    "ARC_NOT_CONFIGURED",
    "ARC_NO_ACTIVE_SCOPE",
    "ARC_WORKING_SET_TOO_SMALL",
    "AUDIT_AUTO_FAILED",
    "AUDIT_INPUT_OVER_WINDOW",
    "AUDIT_PERSIST_FAILED",
    "AUDIT_STAGED_FLUSH_AT_CLOSE",
    "AUDIT_STAGED_FLUSH_FAILED",
    "PLACEMENT_APPENDED",
    "PLACEMENT_STAGED",
    "SKIP_CHECKPOINT_ALREADY_STAGED",
    "SKIP_MODEL_CONTEXT_EMPTY",
    "SKIP_SESSION_HAS_NO_MESSAGES",
    "CompactionError",
    "append_checkpoint",
    "compact_session_context",
    "flush_staged_checkpoint",
    "maybe_autocompact",
    "stage_checkpoint",
    "staged_checkpoint",
]

# ---------------------------------------------------------------------------
# Typed catalog (#775 no-silent-fallback ground rule) -- every degraded/skip
# path below is one of these, never an ad-hoc string.
# ---------------------------------------------------------------------------

#: Skip reasons: ``compact_session_context`` returns ``{"compacted": False, "reason": ...}``.
SKIP_SESSION_HAS_NO_MESSAGES = "session_has_no_messages"
SKIP_MODEL_CONTEXT_EMPTY = "model_context_empty"
SKIP_CHECKPOINT_ALREADY_STAGED = "checkpoint_already_staged"

#: ``arc_status`` values on the compaction memory event (frozen wire surface).
ARC_NOT_CONFIGURED = "not_configured"
ARC_NO_ACTIVE_SCOPE = "no_active_scope"
ARC_WORKING_SET_TOO_SMALL = "working_set_too_small"
ARC_FOLDED = "folded"
ARC_STATUSES = frozenset(
    {ARC_NOT_CONFIGURED, ARC_NO_ACTIVE_SCOPE, ARC_WORKING_SET_TOO_SMALL, ARC_FOLDED}
)

#: ``checkpoint_placement`` values.
PLACEMENT_APPENDED = "appended"
PLACEMENT_STAGED = "staged_for_finalize"

#: Audit reasons (``stream_audit`` stage names double as the reason).
AUDIT_INPUT_OVER_WINDOW = "compaction.input_over_window"
AUDIT_AUTO_FAILED = "compaction.auto_failed"
AUDIT_STAGED_FLUSH_AT_CLOSE = "compaction.staged_flush_at_close"
AUDIT_PERSIST_FAILED = "compaction.persist_failed"
AUDIT_STAGED_FLUSH_FAILED = "compaction.staged_flush_failed"

_BOUNDED_CHARS = 300
_TEXT_PART_TYPES = frozenset({"text", "thinking", "error"})


class CompactionError(Exception):
    """One typed error surface for both triggers.

    The manual route turns this straight into an HTTP response
    (``HTTPException(exc.status, exc.envelope())``); the auto trigger catches it and
    audits :data:`AUDIT_AUTO_FAILED` instead of raising into the turn loop.
    """

    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
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


def _skip(sid: str, reason: str) -> dict[str, Any]:
    return {"session_id": sid, "compacted": False, "reason": reason}


def _typed_persist_error(exc: BaseException, *, event_id: str, stage: str) -> CompactionError:
    """Wrap ANY exception raised while landing a checkpoint as the ONE typed 500 a
    manual-route caller sees (#1339 review F1).

    Both the ARC fold (``_fold_arc_working_set``, an ``arc.summarize_segments`` store
    RPC) and the checkpoint landing (``append_checkpoint``, an atom-mint store RPC) can
    raise a real store defect; before this wrap, that raw exception reached
    ``routes/sessions.py``'s generic error middleware as an UNTYPED 500
    ``internal_error``, exactly the silent-fallback shape #772 forbids. ``stage``
    distinguishes the two landing points in the audit row and the error's ``details``.
    """

    stream_audit(AUDIT_PERSIST_FAILED, event_id=event_id, landing_stage=stage, error=repr(exc))
    return CompactionError(
        500,
        "memory_update_failed",
        f"checkpoint persist failed: {exc!r}",
        {"event_id": event_id, "stage": stage},
        recoverable=False,
    )


# ---------------------------------------------------------------------------
# Transcript rendering -- every part class the client-facing judge already
# renders, not text only, so the summary can stand in for what it covers.
# ---------------------------------------------------------------------------


def _bounded(text: str, limit: int = _BOUNDED_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _bounded_tool_call_fact(part: Any) -> str:
    name = getattr(part, "tool_name", "") or ""
    args = getattr(part, "input", {}) or {}
    return f"tool call: {name}({_bounded(str(args))})"


def _bounded_tool_result_fact(part: Any) -> str:
    name = getattr(part, "tool_name", "") or ""
    status = "error" if getattr(part, "is_error", False) else "ok"
    output = ""
    content = getattr(part, "content", None) or []
    if content:
        output = getattr(content[0], "text", "") or ""
    if not output:
        structured = getattr(part, "structured_content", None)
        if structured is not None:
            output = str(structured)
    return f"tool result: {name} {status}: {_bounded(output)}"


def _part_line(part: Any) -> str:
    part_type = getattr(part, "type", "")
    if part_type == "compaction":
        return (getattr(part, "summary", "") or "").strip()
    if part_type in _TEXT_PART_TYPES:
        return (getattr(part, "text", "") or "").strip()
    if part_type == "tool_call":
        return _bounded_tool_call_fact(part)
    if part_type == "tool_result":
        return _bounded_tool_result_fact(part)
    return ""


def _build_transcript(model_messages: list[Message]) -> str:
    """Render the model context to a transcript blob -- no deterministic truncation;
    the checkpoint IS the bound (#1339); an over-window input surfaces typed
    (:data:`AUDIT_INPUT_OVER_WINDOW`), never a silent drop of older rows."""

    lines: list[str] = []
    for message in model_messages:
        role = (getattr(message, "role", "user") or "user").upper()
        for part in getattr(message, "parts", []) or []:
            line = _part_line(part)
            if line:
                lines.append(f"{role}: {line}")
    return "\n".join(lines)


_PROMPT_RULES = (
    "Create an evidence-preserving compact memory for the following CLIO "
    "conversation transcript. This becomes the next model-context checkpoint, "
    "so preserve concrete scientific evidence, not just a high-level story.\n\n"
    "Rules:\n"
    "- Keep exact file paths, dataset names, column names, variable names, "
    "units, dimensions, counts, statistics, artifact paths, and error messages "
    "when they appear in the transcript.\n"
    "- Preserve which findings came from which source, grouped by file/provider "
    "or workflow stage.\n"
    "- Preserve unresolved gaps, failed inspections, missing dependencies, and "
    "next checks.\n"
    "- If evidence is missing or a source was not inspected, say that explicitly. "
    "Do not fill gaps with plausible details.\n"
    "- Do not invent dataset names, columns, statistics, compression settings, "
    "or readiness conclusions that are not supported by the transcript.\n"
    "- Prefer concise structured bullets over prose. Keep the summary compact, "
    "but do not omit identifiers needed for a later expert to continue the work."
)


def _build_prompt(transcript: str, focus: str) -> str:
    prompt = _PROMPT_RULES
    if focus:
        prompt += f"\n\nFocus the summary on: {focus}"
    prompt += f"\n\n--- transcript ---\n{transcript}\n--- end ---"
    return prompt


# ---------------------------------------------------------------------------
# ARC fold -- meaning only inside a turn's live plane (see module docstring).
# ---------------------------------------------------------------------------


def _fold_arc_working_set(app: Any, summary: str, turn_id: str) -> str:
    """Fold the live ARC working set into ``summary`` when a scope is live.

    Returns one of :data:`ARC_STATUSES`. A MANUAL compact issued while a turn is
    running still resolves :data:`ARC_NO_ACTIVE_SCOPE` here (see the module
    docstring): this function reads the CALLING THREAD's contextvars, and a manual
    compact runs on the route's off-loop executor thread, which carries none --
    only the auto trigger (inside ``instrumented_forward`` itself) ever observes a
    live scope to fold.

    CAN raise: ``arc.summarize_segments`` is a real store RPC and a store defect is
    not this function's contract to hide -- it propagates to the caller
    (:func:`compact_session_context`), which wraps it into the one typed
    ``CompactionError`` a manual-route caller sees (:func:`_typed_persist_error`,
    #1339 review F1).
    """

    top_arc = getattr(getattr(app, "state", None), "arc", None)
    if top_arc is None:
        return ARC_NOT_CONFIGURED

    from clio_agent.gact.agents.reactv2_events import _arc_scope  # noqa: PLC0415

    arc, session, scope = _arc_scope()
    if not scope or arc is None:
        return ARC_NO_ACTIVE_SCOPE
    live = arc.render_working_set(session, scope)
    if len(live) <= 1:
        return ARC_WORKING_SET_TOO_SMALL
    arc.summarize_segments(
        session,
        scope,
        [s.id for s in live],
        {"text": summary},
        token_count=_estimate_text_tokens(summary),
        turn_id=turn_id,
    )
    return ARC_FOLDED


# ---------------------------------------------------------------------------
# The one operation.
# ---------------------------------------------------------------------------


def compact_session_context(
    app: Any, sid: str, *, trigger: Literal["manual", "auto"], focus: str = ""
) -> dict[str, Any]:
    """Compact ``sid``'s session context into ONE appended checkpoint. Blocking,
    off-loop only (every step below is a store RPC and/or an LM call).

    Args:
        app: The FastAPI app.
        sid: The session being compacted.
        trigger: ``"manual"`` for ``POST /compact``, ``"auto"`` for the proactive
            threshold trigger.
        focus: Optional user-supplied focus instructions (manual only).

    Returns:
        On success: ``{session_id, compacted: True, event_id, archived_count,
        summary, checkpoint_placement}``. On a typed skip:
        ``{session_id, compacted: False, reason}``.

    Raises:
        CompactionError: session not found (404), no LM agent wired (503), the LM
            call failed after retries (502), or the ARC fold / checkpoint landing
            raised a store defect (500 ``memory_update_failed``, #1339 review F1 --
            never an untyped 500).
    """

    sess = app.state.sessions.get(sid)
    if sess is None:
        raise CompactionError(404, "not_found", f"session not found: {sid}", {"session_id": sid})

    ledger = list(app.state.messages.get(sid, []))
    if not ledger:
        return _skip(sid, SKIP_SESSION_HAS_NO_MESSAGES)

    model_messages = model_context_messages(ledger)
    if not model_messages:
        return _skip(sid, SKIP_MODEL_CONTEXT_EMPTY)

    transcript = _build_transcript(model_messages)
    if not transcript.strip():
        # #1339 review F2: a session whose model-context rows render to nothing (e.g.
        # only a2ui/mcp_app parts with no text-bearing class) has real work to skip,
        # not an empty LLM call -- checked before dispatch_pre_compact/the LLM so
        # neither ever sees an empty ``--- transcript ---`` block. Otherwise
        # unreachable in steady state: the latest checkpoint row always renders (its
        # ``summary`` line) once one exists.
        return _skip(sid, SKIP_MODEL_CONTEXT_EMPTY)

    input_tokens = _estimate_text_tokens(transcript)
    context_window = _ctx.active_react_context_window()
    if not context_window:
        cfg = getattr(getattr(app.state, "agent", None), "_provider_config", None)
        context_window = _resolve_expert_context_window(cfg) if cfg is not None else 0
    if context_window and input_tokens > context_window:
        stream_audit(
            AUDIT_INPUT_OVER_WINDOW,
            session_id=sid,
            trigger=trigger,
            input_tokens=input_tokens,
            context_window=context_window,
        )

    from clio_agent.gact.hooks import dispatch_pre_compact  # noqa: PLC0415

    dispatch_pre_compact(
        session_id=sid,
        cwd=str(getattr(sess, "workspace_root", "") or ""),
        payload={
            "message_count": len(model_messages),
            "transcript_chars": len(transcript),
            "trigger": trigger,
        },
    )

    agent = getattr(app.state, "agent", None)
    if agent is None:
        raise CompactionError(
            503,
            "agent_unavailable",
            "no LM agent wired; configure one via PUT /v1/providers/lm",
        )

    prompt = _build_prompt(transcript, focus)

    def _summarize() -> str:
        return agent._run_chat_agent(prompt, "")

    retry_call = getattr(agent, "_call_with_transient_provider_retries", None)
    try:
        summary = (
            retry_call("compact_summary", _summarize) if callable(retry_call) else _summarize()
        )
    except Exception as exc:  # noqa: BLE001 - surfaced typed below, never silently dropped
        raise CompactionError(
            502, "upstream_error", f"compact summarisation failed: {exc!r}"
        ) from exc

    evidence_index = _compact_exact_evidence_index(transcript)
    if evidence_index:
        summary = (summary or "").rstrip() + "\n\n" + evidence_index

    event_id = _new_memory_event_id()
    turn_id = _active_semantic_turn_id()
    checkpoint = build_compact_summary_message(
        session_id=sid,
        turn_id=turn_id,
        summary=summary or "",
        event_id=event_id,
        compacted_message_ids=[m.id for m in model_messages],
        auto=(trigger == "auto"),
    )

    try:
        arc_status = _fold_arc_working_set(app, summary or "", turn_id)
    except Exception as exc:  # noqa: BLE001 - typed below (#1339 review F1)
        raise _typed_persist_error(exc, event_id=event_id, stage="fold_arc_working_set") from exc
    if arc_status == ARC_FOLDED:
        from clio_agent.providers.stateful_common import (  # noqa: PLC0415
            note_prefix_reset_for_active_scope,
        )

        note_prefix_reset_for_active_scope("ops_reset")

    fields: dict[str, Any] = {
        "event_id": event_id,
        "archived_count": len(model_messages),
        "arc_status": arc_status,
        "trigger": trigger,
        "input_tokens_estimated": input_tokens,
        "context_window": context_window,
        "summary": summary,
    }

    from clio_agent.gact.part_atom_minter import turn_minter  # noqa: PLC0415

    if turn_minter(app, sid) is not None:
        if staged_checkpoint(app, sid) is not None:
            return _skip(sid, SKIP_CHECKPOINT_ALREADY_STAGED)
        return stage_checkpoint(app, sid, checkpoint, **fields)
    try:
        return append_checkpoint(app, sid, checkpoint, **fields)
    except Exception as exc:  # noqa: BLE001 - typed below (#1339 review F1)
        raise _typed_persist_error(exc, event_id=event_id, stage="append_checkpoint") from exc


def append_checkpoint(
    app: Any,
    sid: str,
    checkpoint: Message,
    *,
    event_id: str,
    archived_count: int,
    arc_status: str,
    trigger: str,
    input_tokens_estimated: int,
    context_window: int,
    summary: str,
) -> dict[str, Any]:
    """The single landing point for a checkpoint row. Blocking, off-loop only.

    Appends the checkpoint to the ledger (the a2ui mid-turn pattern: the ledger +
    per-session file write here, the atoms through the minter FIFO when a turn is
    open, else inline off-loop), records the memory event, and publishes both
    ``message.created`` (the only way the web client learns of the new row -- the
    v3 ``message.upserted`` projector) and ``session.compacted``.

    A failure below the ledger/file write (the atom mint, via ``run_transcript_job``
    -> ``on_message_appended``) can still raise: the checkpoint row is then ALREADY
    in the in-memory ledger and the per-session ``MessageStore`` file -- that file is
    the durable copy, and the atom lane's own backfill
    (``transcript_projection.mint_atoms_from_ledger``) repairs the projection from it
    on the next read, so no data is lost, only the atom mint is deferred. Every
    caller (the immediate manual-route landing, and :func:`flush_staged_checkpoint`)
    is responsible for turning that raise into ITS OWN typed handling -- this
    function itself raises whatever the write seams raise, untyped.
    """

    from clio_agent.gact.part_atom_minter import run_transcript_job  # noqa: PLC0415
    from clio_agent.gact.session_store import _append_session_message  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import on_message_appended  # noqa: PLC0415

    _append_session_message(app, sid, checkpoint, atoms_minted=True)
    run_transcript_job(
        app,
        sid,
        f"compaction:{event_id}",
        lambda: on_message_appended(app, sid, checkpoint),
    )
    app.state.sessions.update(sid, message_count=len(app.state.messages.get(sid, [])))

    now = datetime.now(timezone.utc).isoformat()
    summary_chars = len(summary or "")
    memory_event = {
        "id": event_id,
        "version": 1,
        "type": "compact_summary",
        "session_id": sid,
        "created_at": now,
        "updated_at": now,
        "summary_message_id": checkpoint.id,
        "archived_count": archived_count,
        "summary_chars": summary_chars,
        "arc_status": arc_status,
        "trigger": trigger,
        "checkpoint_placement": PLACEMENT_APPENDED,
        "input_tokens_estimated": input_tokens_estimated,
        "context_window": context_window,
        "metadata": {
            "source": "gact_compact",
            "synthetic": "compact_summary",
            "evidence_index": "[exact retained evidence index]" in (summary or ""),
        },
    }
    app.state.memory_events.setdefault(sid, []).append(memory_event)
    _emit_semantic_event(
        app,
        sid,
        "memory.compacted",
        turn_id=_ctx.active_turn_id(),
        trace_id=_ctx.active_trace_id(),
        summary="Session transcript was compacted into memory.",
        actor={"role": "runtime", "component": "memory"},
        subject={"memory_event_id": event_id},
        payload=memory_event,
    )

    app.state.bus.publish(
        Event(type="message.created", session_id=sid, payload=checkpoint.to_wire())
    )
    app.state.bus.publish(
        Event(
            type="session.compacted",
            session_id=sid,
            payload={
                "event_id": event_id,
                "archived_count": archived_count,
                "summary_chars": summary_chars,
                "summary_message_id": checkpoint.id,
                "version": 1,
                "trigger": trigger,
            },
        )
    )
    return {
        "session_id": sid,
        "compacted": True,
        "event_id": event_id,
        "archived_count": archived_count,
        "summary": summary,
        "checkpoint_placement": PLACEMENT_APPENDED,
    }


# ---------------------------------------------------------------------------
# Staging -- a checkpoint built while a turn's minter is open waits for that
# turn's assistant message to persist first (never inserted ahead of it).
# ---------------------------------------------------------------------------


def _staged_compactions(app: Any) -> dict[str, dict[str, Any]]:
    staged = getattr(app.state, "staged_compactions", None)
    if staged is None:
        staged = {}
        app.state.staged_compactions = staged
    return staged


def stage_checkpoint(app: Any, sid: str, checkpoint: Message, **fields: Any) -> dict[str, Any]:
    """Hold ``checkpoint`` until the open turn's assistant message persists."""

    staged = _staged_compactions(app)
    staged[sid] = {"checkpoint": checkpoint, **fields}
    return {
        "session_id": sid,
        "compacted": True,
        "event_id": fields["event_id"],
        "archived_count": fields["archived_count"],
        "summary": fields["summary"],
        "checkpoint_placement": PLACEMENT_STAGED,
    }


def staged_checkpoint(app: Any, sid: str) -> Optional[dict[str, Any]]:
    """The session's pending staged checkpoint entry, or ``None``."""

    return _staged_compactions(app).get(sid)


def flush_staged_checkpoint(app: Any, sid: str) -> Optional[dict[str, Any]]:
    """Append the session's staged checkpoint, if any. ``None`` when nothing is staged.

    CAN raise (see :func:`append_checkpoint`'s docstring): a persist failure here
    must never fail the turn whose finalize triggered the flush, so both callers
    (``part_atom_minter``'s ``persist_finalized_message`` primary flush and
    ``close_turn_minter`` backstop) catch around this call, audit
    :data:`AUDIT_STAGED_FLUSH_FAILED`, and continue (#1339 review F1) -- this
    function itself does not swallow anything.
    """

    staged = _staged_compactions(app)
    entry = staged.pop(sid, None)
    if entry is None:
        return None
    checkpoint = entry.pop("checkpoint")
    return append_checkpoint(app, sid, checkpoint, **entry)


# ---------------------------------------------------------------------------
# Auto trigger.
# ---------------------------------------------------------------------------


def maybe_autocompact() -> None:
    """Proactive, threshold-crossing auto-compaction -- the V2 trigger (#901 S6,
    unified onto :func:`compact_session_context` by #1339).

    When ``prompt_tokens / context_window`` crosses the session's configured
    threshold, run the SAME operation manual compaction uses, with
    ``trigger="auto"``. A failure is audited (:data:`AUDIT_AUTO_FAILED`) and
    swallowed -- the loop continues, backstopped by the existing
    ``ContextWindowExceededError`` handling; auto-compaction is a proactive
    optimization, never a hard turn dependency.
    """

    from clio_agent.gact.agents.reactv2_events import _arc_scope  # noqa: PLC0415
    from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
        _last_prompt_tokens,
        _session_autocompact_preferences,
    )

    arc, session, _scope = _arc_scope()
    if arc is None:
        return
    app = _ctx.active_app()
    if app is None:
        # #1339 review round: active_app() is documented nullable
        # (gact/context.py); compact_session_context requires a real app
        # (it reads app.state.sessions unguarded) -- never a hard crash for a
        # proactive optimization the docstring itself promises is optional.
        return
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session_row = sessions.get(session) if sessions is not None else None
    enabled, threshold = _session_autocompact_preferences(getattr(session_row, "metadata", None))
    if not enabled:
        return
    window = _ctx.active_react_context_window()
    last = _last_prompt_tokens()
    if not window or not last:
        return
    if (last / window) < threshold:
        return
    try:
        compact_session_context(app, session, trigger="auto")
    except CompactionError as exc:
        stream_audit(
            AUDIT_AUTO_FAILED,
            session_id=session,
            error=exc.error,
            message=exc.message[:300],
        )
