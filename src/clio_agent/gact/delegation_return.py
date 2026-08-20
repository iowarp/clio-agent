"""Delegation-return stamping — the child's final answer carries its return edge.

When a delegation (an :class:`~clio_agent.gact.agent_tasks.AgentTask`) reaches a
terminal state, the CHILD session's final assistant message — the one whose text
the parent collects as the delegation's output (``result.message_ref``, resolved
verbatim at collect time) — must CARRY that fact on the wire, so a UI reading
``GET /v1/sessions/{child_sid}/messages`` renders it with return-to-parent
semantics without inferring anything (owner, 2026-08-05).

The stamp is message METADATA on the persisted record::

    metadata.delegation_return = {
        "parent_session_id": <the parent session the output returns to>,
        "task_id":           <the AgentTask id of this delegation>,
        "parent_agent":      <the requesting (parent) expert id, e.g. "main">,
    }

Target selection: the message named by the sealed ``result.message_ref`` when
present. When the ref is absent on a path (a relay-folded / forwarded record),
the newest non-live assistant message of the child session AT TERMINAL TIME is
the honest fallback; a ref that names a GONE message, or a child with NO
assistant message, stamps NOTHING (typed log) rather than mark the wrong message.

Exactly-once: stamping is IDEMPOTENT per task — re-stamping the same ``task_id``
is a no-op — so the two call seams (the terminal fold's winner effects in
:func:`~clio_agent.gact.task_fold.finish_agent_task_transition` and the
once-per-task collect emission in ``spawn_runtime._emit_delegation_terminal``)
never duplicate. This is a persisted-record PATCH: no message event is
(re)published for old sessions — ``message.part.updated`` is the live-turn PART
idiom owned by the open TurnTranscript (``transcript.py``), not a settled-ledger
metadata idiom — so the stamp persists silently and rides the next ``GET``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.session_store import _replace_session_messages

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

#: The message-metadata key carrying the return-to-parent stamp.
DELEGATION_RETURN_KEY = "delegation_return"


def stamp_delegation_return(app: "FastAPI", task: Any) -> bool:
    """Stamp ``task``'s child final assistant message with its return-to-parent edge.

    Args:
        app: The GACT app (session/message stores on ``app.state``).
        task: A terminal task-like record (:class:`AgentTask` or the invoker's
            ``TaskResult`` wire projection) carrying ``task_id`` /
            ``parent_session_id`` / ``child_session_id`` / ``agent_ref`` /
            ``result``.

    Returns:
        ``True`` when a stamp was written and persisted; ``False`` on any no-op
        (already stamped for this task, no resolvable target message, or a
        best-effort store fault — each logged with a typed reason).

    Never raises: it runs among terminal side effects on the child done-callback
    thread, where an exception would vanish into the callback (no-silent-fallback:
    every degrade is logged with a typed reason instead).
    """

    try:
        return _stamp(app, task)
    except Exception as exc:  # noqa: BLE001 - terminal effects must not crash the done-callback
        logger.warning(
            "delegation_return not stamped reason=stamp_failed task=%s child=%s err=%r",
            getattr(task, "task_id", "?"),
            getattr(task, "child_session_id", "?"),
            exc,
        )
        return False


def _stamp(app: "FastAPI", task: Any) -> bool:
    child_sid = str(getattr(task, "child_session_id", "") or "")
    task_id = str(getattr(task, "task_id", "") or "")
    if not child_sid or not task_id:
        return False
    messages = app.state.messages.get(child_sid, []) or []
    target = _target_message(list(messages), task, child_sid=child_sid, task_id=task_id)
    if target is None:
        return False
    existing = (getattr(target, "metadata", {}) or {}).get(DELEGATION_RETURN_KEY)
    if isinstance(existing, Mapping):
        if str(existing.get("task_id", "")) == task_id:
            return False  # already stamped for this task — idempotent no-op
        # One child session maps to exactly one task, so a different claimant is a
        # defect somewhere upstream — refuse to clobber, loudly.
        logger.warning(
            "delegation_return not stamped reason=message_already_claimed task=%s "
            "child=%s message=%s claimed_by=%s",
            task_id,
            child_sid,
            getattr(target, "id", "?"),
            existing.get("task_id", ""),
        )
        return False
    agent_ref = getattr(task, "agent_ref", None) or {}
    target.metadata = {
        **(getattr(target, "metadata", {}) or {}),
        DELEGATION_RETURN_KEY: {
            "parent_session_id": str(getattr(task, "parent_session_id", "") or ""),
            "task_id": task_id,
            "parent_agent": str(agent_ref.get("requesting_expert_id", "") or ""),
        },
    }
    # Memory + disk in lock step (and the atom lane under the atoms regime): the
    # stamped copy is what GET /v1/sessions/{child_sid}/messages serves afterwards.
    _replace_session_messages(app, child_sid, list(messages))
    return True


def _target_message(
    messages: list["Message"], task: Any, *, child_sid: str, task_id: str
) -> Optional["Message"]:
    """Resolve the child message the stamp targets, or ``None`` (emit nothing)."""

    result = getattr(task, "result", None)
    result = result if isinstance(result, Mapping) else {}
    message_ref = str(result.get("message_ref", "") or "")
    if message_ref:
        target = next((m for m in messages if getattr(m, "id", "") == message_ref), None)
        if target is None:
            # The sealed result NAMES the final message but it is gone (pruned
            # child ledger): stamping any other message would mark the WRONG one —
            # emit nothing, with the same typed reason the verbatim-output
            # resolution uses for this degradation.
            logger.warning(
                "delegation_return not stamped reason=child_message_gone task=%s "
                "child=%s message_ref=%s",
                task_id,
                child_sid,
                message_ref,
            )
        return target
    # message_ref absent on this path (a relay-folded / forwarded-task result): the
    # newest non-live assistant message of the child session at terminal time is the
    # honest fallback — and when the child has NO assistant message at all we emit
    # NOTHING rather than stamp the wrong message.
    finals = [
        m
        for m in messages
        if getattr(m, "role", "") == "assistant"
        and not ((getattr(m, "metadata", {}) or {}).get("live"))
    ]
    return finals[-1] if finals else None
