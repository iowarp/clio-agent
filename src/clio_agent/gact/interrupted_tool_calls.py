"""Tool calls a restart interrupted before their outcome was recorded (turn recovery).

A tool call's start is recorded as a ``tool.call.started`` event on the session's
``_events`` log, behind the call (:mod:`clio_agent.gact.highway_writer`: within one
writer group, before the next model request; a start still queued when the process
stops is lost with it, and that call is not reported). Its transcript ``tool_call`` part is sealed only when its
result arrives (#1337), and a read-only MCP call's task record is written behind its
reply (:mod:`clio_agent.gact.read_only_outcomes`). So a process that stops while a call
runs, or just after it answered, can leave that start as the call's only record: no
transcript part and no task.

Turn recovery closes the interrupted turn (:mod:`clio_agent.gact.restart_interruptions`):
with one assistant row after a dangling user message
(``session_store._interrupted_assistant_row``), or on the turn's own incomplete
assistant message when its sealed parts are the transcript's last row.
:func:`unrecorded_tool_calls` gives that row a ``tool_call`` part for each call of the turn that started and has neither
a recorded completion (``tool.call.completed`` / ``tool.call.failed``) nor a part in
the row. The part is built from the recorded start only and carries the typed reason
``tool_outcome_unrecorded`` in ``metadata.outcome``; with no result part in a turn that
ended, the transcript reports the call's outcome as unknown
(:mod:`clio_agent.gact.protocol.v3.tool_outcome`), never as success and never by
leaving it out. Nothing is resubmitted.

The reverse loss happens too: a call whose part was sealed and whose completion was
recorded, while its ``tool_result`` part was still queued when the process stopped.
:func:`restored_tool_results` gives the row that result, rebuilt from the recorded
completion (its result, ok and presentation) and marked
``metadata.outcome.reason = "tool_result_restored"``. A started flow keeps its card and
handle this way; the outcome is the one recorded, not an inferred one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from clio_agent.gact.parts import Part

if TYPE_CHECKING:
    from clio_agent.gact.types import Message

__all__ = [
    "TOOL_OUTCOME_UNRECORDED",
    "TOOL_RESULT_RESTORED",
    "restored_tool_results",
    "unrecorded_tool_calls",
    "with_unrecorded_calls",
]

logger = logging.getLogger(__name__)

#: Typed reason: a call of an interrupted turn started and its outcome was never recorded.
TOOL_OUTCOME_UNRECORDED = "tool_outcome_unrecorded"
#: Typed reason: a call's result part was lost, and it was rebuilt from its recorded completion.
TOOL_RESULT_RESTORED = "tool_result_restored"
_ENDED = frozenset({"tool.call.completed", "tool.call.failed"})


def _turn_calls(
    arc: Any, session_id: str, turn_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """The turn's recorded call starts and endings, by call id (the ``_events`` log, once)."""
    started: dict[str, dict[str, Any]] = {}
    ended: dict[str, dict[str, Any]] = {}
    for segment in arc._live.iter_session_event_segments(session_id):
        if str(segment.turn_id or "") != turn_id:
            continue
        content = segment.content
        call_id = _call_id(content)
        if not call_id:
            continue
        if content.get("event_type") == "tool.call.started":
            started.setdefault(call_id, content)
        elif content.get("event_type") in _ENDED:
            ended.setdefault(call_id, content)
    return started, ended


def _call_id(content: dict[str, Any]) -> str:
    payload = content.get("payload") or {}
    subject = content.get("subject") or {}
    return str(payload.get("call_id") or subject.get("call_id") or "")


def unrecorded_tool_calls(
    arc: Any, session_id: str, turn_id: str, present: Iterable[str]
) -> list[Part]:
    """The ``tool_call`` parts an interrupted turn's row lacks (module docstring).

    Args:
        arc: The canonical-log ARC (its ``_events`` log is read once).
        session_id: The interrupted conversation.
        turn_id: The interrupted turn (its user message id).
        present: Call ids the row already holds a part for.

    Returns:
        One part per call that started in the turn with no completion recorded, in
        start order.
    """
    started, ended = _turn_calls(arc, session_id, turn_id)
    known = set(present) | set(ended)
    parts: list[Part] = []
    for call_id, content in started.items():
        if call_id in known:
            continue
        payload = content.get("payload") or {}
        tool = str(payload.get("tool") or (content.get("actor") or {}).get("tool") or "")
        logger.warning(
            "Turn recovery reason=%s session=%s turn=%s call=%s tool=%s",
            TOOL_OUTCOME_UNRECORDED,
            session_id,
            turn_id,
            call_id,
            tool,
        )
        args = payload.get("args")
        parts.append(
            Part(
                id=f"live_{call_id}_call",
                type="tool_call",
                call_id=call_id,
                tool_name=tool,
                tool_title=str(payload.get("tool_title") or ""),
                input=args if isinstance(args, dict) else {},
                metadata={
                    "started_at": str(payload.get("started_at") or content.get("occurred_at")),
                    "outcome": {"reason": TOOL_OUTCOME_UNRECORDED, "recorded": "started"},
                    **(
                        {"parent_call_id": str(payload["parent_call_id"])}
                        if payload.get("parent_call_id")
                        else {}
                    ),
                },
            )
        )
    return parts


def restored_tool_results(arc: Any, session_id: str, turn_id: str, row: Message) -> list[Part]:
    """Result parts for ``row``'s sealed calls whose completion was recorded (module docstring).

    Returns:
        One ``tool_result`` part per ``tool_call`` part in ``row`` that has no result
        part and a recorded ``tool.call.completed`` / ``tool.call.failed`` event.
    """
    from clio_agent.gact.tool_observer_parts import (  # noqa: PLC0415
        completed_tool_metadata,
        completed_tool_text,
    )

    answered = {part.call_id for part in row.parts if part.type == "tool_result"}
    waiting = [
        part
        for part in row.parts
        if part.type == "tool_call" and part.call_id and part.call_id not in answered
    ]
    if not waiting:
        return []
    _, ended = _turn_calls(arc, session_id, turn_id)
    parts: list[Part] = []
    for call in waiting:
        content = ended.get(call.call_id)
        if content is None:
            continue
        payload = content.get("payload") or {}
        ok = bool(payload.get("ok", content.get("event_type") == "tool.call.completed"))
        error = payload.get("error")
        result = payload.get("result")
        presentation = payload.get("presentation")
        logger.warning(
            "Turn recovery reason=%s session=%s turn=%s call=%s tool=%s",
            TOOL_RESULT_RESTORED,
            session_id,
            turn_id,
            call.call_id,
            call.tool_name,
        )
        parts.append(
            Part(
                id=f"live_{call.call_id}_result",
                type="tool_result",
                agent_id=call.agent_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                is_error=not ok,
                duration_ms=float(payload.get("duration_ms") or 0.0),
                presentation=presentation if isinstance(presentation, dict) else None,
                content=[
                    Part(
                        id=f"live_{call.call_id}_result_text",
                        type="text",
                        agent_id=call.agent_id,
                        text=completed_tool_text(result, str(error) if error else None),
                    )
                ],
                metadata={
                    **completed_tool_metadata(
                        result,
                        completed_at=str(payload.get("completed_at") or content.get("occurred_at")),
                        cancellation={},
                        provenance={},
                    ),
                    "outcome": {"reason": TOOL_RESULT_RESTORED, "recorded": "completed"},
                },
            )
        )
    return parts


def with_unrecorded_calls(arc: Any, session_id: str, turn_id: str, row: Message) -> Message:
    """``row`` (the interrupted turn's closing row) with its recorded outcomes completed.

    Each sealed call keeps its recorded result (:func:`restored_tool_results`, placed
    after its call), and each call that started without a part is appended as unknown.
    """
    restored = {part.call_id: part for part in restored_tool_results(arc, session_id, turn_id, row)}
    parts: list[Part] = []
    for part in row.parts:
        parts.append(part)
        if part.type == "tool_call" and part.call_id in restored:
            parts.append(restored.pop(part.call_id))
    present = [part.call_id for part in parts if part.call_id]
    calls = unrecorded_tool_calls(arc, session_id, turn_id, present)
    if not calls and len(parts) == len(row.parts):
        return row
    return row.model_copy(update={"parts": [*parts, *calls]})
