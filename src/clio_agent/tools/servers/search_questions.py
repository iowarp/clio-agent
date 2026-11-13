"""Turn search's person questions into MCP input-required rounds and back.

A search tool runs its body through :func:`answered_run`. When the body needs the
person (:class:`clio_agent.search.broker.AskPerson`) the tool returns an MCP
``InputRequiredResult``: one form question whose single choice field CLIO shows
as a choice question. The client asks the person and calls the tool again with
the answer; this module records it (for every waiting caller) and reruns the
body, which now finds the answer and continues. Search bodies decide before they
call a source, so a rerun never repeats a source call.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import mcp_types
from fastmcp import Context

from clio_agent.search import broker
from clio_agent.search.reasons import note
from clio_agent.search.web import SearchFailure

_FIELD = "choice"
_REQUEST = "search_decision"


def request_key(topic: str) -> str:
    """The input key for ``topic``'s question, distinct per decision.

    One call can ask several questions in turn (decline Keenable, then Exa asks).
    MCP refuses an input key reused with different content, so each decision's
    question has its own key (2026-11-13: "Inline input identity was reused with
    changed content" left the second source unasked).
    """

    return f"{_REQUEST}:{topic}"


def input_required(decision: broker.Decision) -> mcp_types.InputRequiredResult:
    """The MCP round that asks the person ``decision``."""

    values = [value for value, _ in decision.options]
    labels = [label for _, label in decision.options]
    schema = {
        "type": "object",
        "properties": {
            _FIELD: {"type": "string", "title": "Choice", "enum": values, "enumNames": labels}
        },
        "required": [_FIELD],
    }
    request = mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(message=decision.message, requested_schema=schema)
    )
    return mcp_types.InputRequiredResult(
        input_requests={request_key(decision.topic): request},
        request_state=json.dumps({"topic": decision.topic}),
        result_type="input_required",
    )


def _refusal_value(topic: str) -> str:
    return broker.DECLINE if topic.startswith("approve:") else broker.STOP


def apply_responses(ctx: Context) -> None:
    """Record the person's answer carried by a retried call, if any."""

    responses = ctx.input_responses
    if not responses:
        return
    try:
        topic = str(json.loads(ctx.request_state or "{}").get("topic") or "")
    except (TypeError, ValueError):
        topic = ""
    if not topic:
        return
    answer = responses.get(request_key(topic))
    action = getattr(answer, "action", "")
    content = getattr(answer, "content", None) or {}
    value = str(content.get(_FIELD) or "") if isinstance(content, dict) else ""
    if action == "accept" and value:
        broker.apply_answer(topic, value)
    elif action == "decline":
        broker.apply_answer(topic, _refusal_value(topic))
    else:
        broker.abandon(topic)


async def answered_run(ctx: Context, body: Callable[[], Awaitable[dict[str, Any]]]) -> Any:
    """Run ``body``; return its result, a typed failure, or the person's question."""

    apply_responses(ctx)
    try:
        return await body()
    except broker.AskPerson as ask:
        return input_required(ask.decision)
    except broker.WaitingForPerson as waiting:
        failure = note("search_waiting_for_person", detail=str(waiting))
        return {"ok": False, "error": failure.as_dict(), "notes": []}
    except SearchFailure as failure:
        return failure.as_result()
    except ValueError as exc:
        return {
            "ok": False,
            "error": {"code": "invalid_arguments", "message": str(exc)},
            "notes": [],
        }


__all__ = ["answered_run", "apply_responses", "input_required"]
