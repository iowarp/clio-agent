"""P2.7 hook audit — ONE semantic event per hook invocation, on the highway (RULE 4).

Every hook invocation — the decision (allow/deny/ask/modify/synthesize/defer), a
denial reason, a hook error/timeout, AND a pre-execution rejection — is audited
exactly once, here, at the single dispatch chokepoint. The audit is NOT a new JSONL
store (#737): it rides the semantic-event highway as a ``hook.invoked`` event
(captured FULL on the durable trace + ARC, queryable after the fact) and, because the
highway needs an active app/session that an out-of-band dispatch may not have, it also
lands in a bounded in-process ring (the SAME pattern as the ``stream_fallback`` /
``hook_reasons`` catalogs) so the record survives even when no turn is bound.

The highway emit resolves the live app from the keystone-bound context
(:func:`clio_agent.gact.context.active_app`) — the same app the turn already binds —
so no process-global app and no ``build_app`` wiring line is needed. A test may
instead install a capturing emitter (:func:`install_hook_audit_emitter`) to assert the
exactly-once contract without standing up ARC.

``SemanticEvent``-event hook invocations are deliberately NOT audited (see
:func:`should_audit`): a ``hook.invoked`` emit is itself a semantic event that would
re-fire the ``SemanticEvent`` observation hooks, and auditing THOSE would recurse.
Observation hooks on the highway carry no governance decision, so skipping their audit
loses nothing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The semantic-event type for one audited hook invocation. Trace-only substrate
#: (declared in :data:`clio_agent.gact.semantic_events.SSE_TRACE_ONLY_EVENT_TYPES`),
#: so it is captured FULL on the durable trace + ARC but never served on the live UI
#: wire — the audit is a queryable-after-the-fact record, not a UI row.
HOOK_INVOKED_EVENT = "hook.invoked"

#: The one hook event whose invocations are NOT audited (recursion guard, see module
#: docstring). Kept as a constant so the guard is greppable and single-sourced.
_SEMANTIC_EVENT = "SemanticEvent"

HookAuditRecord = dict[str, Any]
HookAuditEmitter = Callable[[HookAuditRecord], None]

_EMITTER: HookAuditEmitter | None = None

_RECENT_MAX = 128
_RECENT: "deque[HookAuditRecord]" = deque(maxlen=_RECENT_MAX)
_RECENT_LOCK = threading.Lock()


def install_hook_audit_emitter(emitter: HookAuditEmitter | None) -> None:
    """Install (or clear) a capturing audit emitter (tests). ``None`` restores the
    default highway emit."""

    global _EMITTER
    _EMITTER = emitter


def get_hook_audit_emitter() -> HookAuditEmitter | None:
    """Return the installed capturing emitter, or ``None`` (default highway emit)."""

    return _EMITTER


def recent_hook_invocations() -> list[HookAuditRecord]:
    """Return a snapshot of the bounded recent-invocation ring (for ``GET /v1/hooks``)."""

    with _RECENT_LOCK:
        return list(_RECENT)


def should_audit(event: str) -> bool:
    """Return whether invocations of ``event`` are audited (all but ``SemanticEvent``)."""

    return event != _SEMANTIC_EVENT


def emit_hook_audit(record: HookAuditRecord) -> None:
    """Audit one hook invocation exactly once: append to the ring, then to the highway.

    The bounded ring is the authoritative always-on capture (survives an app-less,
    out-of-band dispatch); the highway ``hook.invoked`` event is the derived, queryable
    projection. A capturing emitter (tests) short-circuits both to the callback. Every
    path is guarded so an audit failure NEVER crashes the dispatch it is observing —
    but it is never silent either (a highway failure is logged with a typed reason).
    """

    with _RECENT_LOCK:
        _RECENT.append(record)
    emitter = _EMITTER
    if emitter is not None:
        try:
            emitter(record)
        except Exception as exc:  # noqa: BLE001 - a capturing emitter must not crash dispatch
            logger.warning("hook audit emitter raised reason=hook_audit_emitter_failed: %r", exc)
        return
    _emit_on_highway(record)


def _emit_on_highway(record: HookAuditRecord) -> None:
    """Emit the ``hook.invoked`` audit event on the semantic highway (best-effort).

    Resolves the live app from the keystone-bound context. When no app/turn is bound
    (a truly out-of-band invocation) there is no session to attribute the highway event
    to — the invocation is still audited in the bounded ring above, so completeness
    holds; the highway is simply the served projection of what a turn produced.
    """

    from clio_agent.gact.context import active_app  # noqa: PLC0415 - avoid import cycle

    app = active_app()
    if app is None:
        return
    decision = str(record.get("decision") or "")
    status = str(record.get("status") or "")
    hook_id = str(record.get("hook_id") or "")
    event = str(record.get("event") or "")
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            str(record.get("session_id") or ""),
            HOOK_INVOKED_EVENT,
            turn_id=str(record.get("turn_id") or ""),
            status="completed",
            summary=f"Hook {hook_id!r} invoked on {event}: {decision or status}.",
            actor={"hook": event, "hook_id": hook_id, "source": record.get("source", "")},
            subject={"tool_name": record.get("tool_name", "")},
            payload=dict(record),
        )
    except Exception as exc:  # noqa: BLE001 - observability, never fatal to a dispatch
        logger.warning(
            "hook audit highway emit skipped reason=hook_audit_emit_failed "
            "hook_id=%s event=%s: %r",
            hook_id,
            event,
            exc,
        )
