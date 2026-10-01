"""Typed error for a ReAct step whose model reply could not be read.

A step ends in a parse failure when the model's reply is neither a tool call nor
the declared output fields: for example, it announces the next step in prose and
stops without making the call. The loop cannot continue from such a reply, and
it used to end the turn with no reason at all, so the user saw "Agent completed
without user-visible output" and the trace lost what the model actually said.

:func:`parse_failure_error_info` keeps that evidence: the adapter's message, the
exception class, and the model's raw reply travel on the prediction's
``error_info``, which the turn adopts as its terminal error (``turn.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clio_agent.gact.types import ErrorInfo

PARSE_FAILURE_ERROR = "model_reply_unparseable"
PARSE_FAILURE_MESSAGE = (
    "The model replied without a tool call or an answer CLIO could read, so the turn "
    "stopped. Send the message again or choose another model."
)


def parse_failure_error_info(exc: Exception, *, step_index: int, expert_id: str) -> "ErrorInfo":
    """Build the terminal error for a step whose reply failed to parse.

    Args:
        exc: The ``AdapterParseError`` or ``ValueError`` raised while reading the
            model's reply.
        step_index: Zero-based index of the step that failed within the turn.
        expert_id: The agent whose loop was running (empty for the main agent).

    Returns:
        A recoverable :class:`~clio_agent.gact.types.ErrorInfo` whose details
        carry the raw reply (``raw_reply``) exactly as the model sent it, or an
        empty string when the exception did not include one.
    """
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    raw_reply = getattr(exc, "lm_response", None)
    return ErrorInfo(
        error=PARSE_FAILURE_ERROR,
        message=PARSE_FAILURE_MESSAGE,
        details={
            "termination_reason": "parse_error",
            "exception": type(exc).__name__,
            "adapter": str(getattr(exc, "adapter_name", "") or ""),
            "parse_message": str(getattr(exc, "message", "") or exc),
            "raw_reply": "" if raw_reply is None else str(raw_reply),
            "step_index": step_index,
            "expert_id": expert_id,
        },
        recoverable=True,
    )
