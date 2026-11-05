"""Hook deferral adapters.

PreToolUse keeps the existing permission enforcement and parked-call events.
Turn-ending approvals delegate to Core QuestionCommands; permission cards are
projections and never the authority for those decisions.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.hooks.wire import record_hook_reason
from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.runtime.permission_actions import allows
from clio_agent.gact.runtime.retention import enforce_dict_bound

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.conversation_assignments import ConversationOutcome

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Row kinds + typed reasons + the durable-surface session.metadata key.          #
# --------------------------------------------------------------------------- #

#: ``permission`` row ``kind`` for a PreToolUse defer parked on the executor thread.
PRETOOL_DEFER_KIND = "pretool_defer"

#: ``permission`` row ``kind`` for a turn-ending defer (Stop / UserPromptSubmit) that
#: suspended the session (no held thread) and resumes as a new turn on resolve.
TURN_DEFER_KIND = "turn_defer"

#: The audit ``reason`` stamped on a pending defer row so the ledger attributes the ask
#: to the defer path (never a silent default), mirroring ``REASON_AI_REVIEW_*``.
REASON_HOOK_DEFER = "hook_defer_pending"

#: ``session.metadata`` key: the durable cross-restart mirror of the pending defers for
#: this session — ``{pid: {kind, event, tool_name, created_at, ...}}`` (no fifth store,
#: the #948 AgentTask projection pattern). Written when a defer parks/suspends and
#: pruned when it resolves/times out, so a restart can see (and a future replay slice
#: rehydrate) what was outstanding.
HOOK_DEFER_PENDING_META = "hook_defer_pending"

#: Turn metadata flag carried on a resumed turn so the re-fired turn-ending hook does
#: not re-defer the SAME just-approved yield (the once-gate for turn-ending resume,
#: analogous to plan_exit's constraint-lift). Read by the UserPromptSubmit /Stop seams.
HOOK_DEFER_RESUME_META = "hook_defer_resume"

_DEFER_TIMEOUT_DEFAULT_S = 86400.0  # 24h — a long bound, not "no bound" (thread-occupancy).


def defer_timeout_s() -> float:
    """Return the configurable bound a parked PreToolUse defer waits before fail-safe deny.

    Read live from :mod:`clio_agent.conf` (``hooks.defer_timeout`` /
    ``CLIO_HOOKS_DEFER_TIMEOUT``, default 24h) so a deployment or a test can tune it
    without a code change.

    **Thread-occupancy tradeoff (documented, deliberate):** a within-session PreToolUse
    defer parks the tool-executor thread on a ``threading.Event`` for up to this bound,
    while the interactive gate parks it until the person answers. The bound is LONG (so a human
    can take their time) but never infinite — an unresolved defer is released and DENIED
    fail-safe (:data:`hook_defer_timeout`), never silently auto-approved and never a
    thread pinned forever. The cross-restart path (which releases the thread entirely
    and resumes by replay) is the substrate answer to a defer that outlives the process.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    value = conf.resolve(
        "hooks.defer_timeout",
        env="CLIO_HOOKS_DEFER_TIMEOUT",
        default=_DEFER_TIMEOUT_DEFAULT_S,
        cast=conf.as_float,
    )
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _DEFER_TIMEOUT_DEFAULT_S
    # A non-positive bound would mean "deny immediately", collapsing defer into deny;
    # clamp to a small positive floor so the pending row is still surfaced+resolvable.
    return seconds if seconds > 0 else _DEFER_TIMEOUT_DEFAULT_S


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_pending_meta(app: "FastAPI", sid: str, pid: str, info: dict[str, Any]) -> None:
    """Mirror a pending defer onto ``session.metadata`` (durable cross-restart surface)."""

    if not sid:
        return
    session = app.state.sessions.get(sid)
    existing = {}
    raw = getattr(session, "metadata", None) if session is not None else None
    if isinstance(raw, Mapping) and isinstance(raw.get(HOOK_DEFER_PENDING_META), Mapping):
        existing = dict(raw[HOOK_DEFER_PENDING_META])
    existing[pid] = info
    app.state.sessions.update(sid, metadata_patch={HOOK_DEFER_PENDING_META: existing})


def _clear_pending_meta(app: "FastAPI", sid: str, pid: str) -> None:
    """Prune a resolved/timed-out defer from the durable ``session.metadata`` mirror."""

    if not sid:
        return
    session = app.state.sessions.get(sid)
    raw = getattr(session, "metadata", None) if session is not None else None
    if not isinstance(raw, Mapping) or not isinstance(raw.get(HOOK_DEFER_PENDING_META), Mapping):
        return
    existing = dict(raw[HOOK_DEFER_PENDING_META])
    if pid in existing:
        existing.pop(pid, None)
        app.state.sessions.update(sid, metadata_patch={HOOK_DEFER_PENDING_META: existing})


def _publish_pending(app: "FastAPI", sid: str, row: dict[str, Any]) -> None:
    """Surface a pending defer so ANY client sees it (the interactive-gate event shape)."""

    if hasattr(app.state, "bus"):
        publish_permission_event(
            app,
            "permission.requested",
            owner_session_id=sid,
            payload=row,
        )
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            "hook.defer.pending",
            status="waiting_user",
            summary=f"A {row.get('kind', 'defer')} hook deferred an operation for out-of-band approval.",
            actor={"hook": row.get("hook_event", "")},
            subject={"permission_id": row.get("id", "")},
            payload={
                "permission_id": row.get("id", ""),
                "kind": row.get("kind", ""),
                "hook_event": row.get("hook_event", ""),
                "reason": row.get("defer_reason", ""),
            },
        )
    except Exception as exc:  # noqa: BLE001 - observability, never fatal to parking a defer
        logger.warning(
            "hook.defer.pending semantic emit skipped reason=defer_emit_failed pid=%s err=%r",
            row.get("id", ""),
            exc,
        )


# --------------------------------------------------------------------------- #
# 1) PreToolUse defer — the headline within-session durable park.               #
# --------------------------------------------------------------------------- #


def park_pretool_defer(
    app: "FastAPI",
    *,
    sid: str,
    name: str,
    args: Mapping[str, Any],
    subject: str,
    outcome: Any,
) -> str:
    """Park a PreToolUse call a hook DEFERRED until it is resolved out-of-band (P2.6).

    Generalizes the interactive gate's parked-``threading.Event`` primitive: persist a
    pending-approval row (kind :data:`PRETOOL_DEFER_KIND`), block the executor thread
    on the row's event with the interactive timeout LIFTED to :func:`defer_timeout_s`,
    and return the resolved decision. Returns ``"allow"`` (the tool runs — the
    interceptor consumes any modify/synthesize the approval carries), a
    ``DenyDecision`` (a typed deny reaches the model), or a fail-safe ``"deny"`` on
    timeout / no-session (never a silent auto-approve).

    Caller contract: the gate has already cleared the per-call intercept stash and
    established (deny beats defer) that no policy denies this call; this function owns
    stashing the final intercept for the SAME-thread interceptor that runs next.
    """

    from clio_agent.gact.hooks.intercept import stash_pre_tool_intercept  # noqa: PLC0415

    if not sid:
        # Fail-safe: a defer with nowhere to persist the approval is a DENY, never a
        # silent auto-approve (mirrors the interactive gate's no-session→deny).
        record_hook_reason("hook_defer_no_session", event="PreToolUse", tool_name=name)
        stash_pre_tool_intercept(None)
        return "deny"

    from clio_agent.gact.permission_timeout import wait_for_permission

    cancelled = app.state.cancel_events.get(sid)
    defer_reason = str(getattr(outcome, "reason", "") or "") or (
        f"tool call {name!r} was deferred by a PreToolUse hook for out-of-band approval"
    )
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    evt = threading.Event()
    row: dict[str, Any] = {
        "id": pid,
        "session_id": sid,
        "kind": PRETOOL_DEFER_KIND,
        "hook_event": "PreToolUse",
        "tool_call": {"tool_name": name, "input": dict(args)},
        "summary": f"{subject} {name!r} deferred by a PreToolUse hook (awaiting out-of-band approval)",
        "defer_reason": defer_reason,
        "created_at": _now_iso(),
        "status": "pending",
        "reason": REASON_HOOK_DEFER,
    }
    _record_pending_meta(
        app,
        sid,
        pid,
        {
            "kind": PRETOOL_DEFER_KIND,
            "hook_event": "PreToolUse",
            "tool_name": name,
            "created_at": row["created_at"],
        },
    )
    # Discovery and immediate human responses must see a recoverable request.
    # A failed metadata write leaves no resolvable row or stranded waiter.
    app.state.permission_events[pid] = evt
    app.state.permissions[pid] = row
    enforce_dict_bound(app, app.state.permissions, "permissions", session_id=sid)
    _publish_pending(app, sid, row)

    decision = wait_for_permission(
        app, pid, row, evt, tool_name=name, cancelled=cancelled, timeout_s=defer_timeout_s()
    )
    _clear_pending_meta(app, sid, pid)
    if decision is not None:
        record_hook_reason(
            "hook_defer_timeout" if row.get("status") == "timeout" else "hook_defer_cancelled",
            event="PreToolUse",
            tool_name=name,
            permission_id=pid,
        )
        stash_pre_tool_intercept(None)
        return decision

    action = str(row.get("action", "deny") or "deny")
    if allows(action):
        _stash_resolution_intercept(row)
        return "allow"

    # deny (or anything non-allow): a typed deny to the model — never a silent drop.
    record_hook_reason("hook_defer_denied", event="PreToolUse", tool_name=name, permission_id=pid)
    stash_pre_tool_intercept(None)
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415

    return DenyDecision(
        str(row.get("defer_deny_message") or "")
        or f"tool call {name!r} was DENIED out-of-band after a PreToolUse defer"
    )


def _stash_resolution_intercept(row: Mapping[str, Any]) -> None:
    """Apply the modify/synthesize an APPROVED defer may carry to the interceptor stash.

    The approver may resolve a deferred tool call with ``allow`` alone (run the tool
    with its original args) OR carry a ``modify`` (``resolution_input``) / ``synthesize``
    (``resolution_result``) the same way a PreToolUse hook's tagged-union does. This
    runs on the parked executor thread right before it returns ``"allow"``, so the
    context-local stash the interceptor reads next belongs to THIS call.
    """

    from clio_agent.gact.hooks.intercept import set_pre_tool_intercept  # noqa: PLC0415
    from clio_agent.tools.tool_hooks import InterceptDecision  # noqa: PLC0415

    if "resolution_result" in row:
        set_pre_tool_intercept(
            InterceptDecision(kind="synthesize", result=row.get("resolution_result"))
        )
        return
    modified = row.get("resolution_input")
    if isinstance(modified, Mapping):
        set_pre_tool_intercept(InterceptDecision(kind="modify", modified_args=dict(modified)))
        return
    set_pre_tool_intercept(None)


# --------------------------------------------------------------------------- #
# 2) Turn-ending defer (Stop / UserPromptSubmit) — suspend + deferred-resume.    #
# --------------------------------------------------------------------------- #


def suspend_turn_defer(
    app: "FastAPI",
    *,
    sid: str,
    hook_event: str,
    reason: str,
    resume_text: str,
    prev_status: str = "running",
    extra: Mapping[str, Any] | None = None,
    completion: ConversationOutcome | None = None,
) -> str | None:
    """Delegate turn-ending approval to the existing Core Question owner."""
    from clio_agent.gact.hooks.turn_approval import suspend

    if not sid:
        return None
    if extra:
        raise ValueError("Turn approval bindings must be captured by their trusted owner")
    return suspend(app, sid, hook_event, reason, resume_text, prev_status, completion=completion)
