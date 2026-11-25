"""Typed errors for a turn that ended without an answer.

The agent loop stops on its own for two reasons that leave no answer: it produced
nothing visible, or its conversation outgrew the model's context window
(``termination_reason="context_window_exceeded"``). Without an error the second
looked like a normal ``end_turn`` after its last tool call, so the person saw tool
results and no reply and nothing said why. Both now reach the message as an
``ErrorInfo`` with the stop reason in its details.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.types import ErrorInfo

CONTEXT_FULL_MESSAGE = (
    "The agent stopped before answering: the conversation no longer fits the model's "
    "context window. Large tool results are the usual cause. Ask for a narrower step, "
    "or start a new conversation from the results so far."
)


def unfinished_turn_error(state: Any, pred: Any, has_operation_output: bool) -> ErrorInfo | None:
    """The error for a turn that stopped without an answer, or ``None``."""
    termination = str(getattr(pred, "termination_reason", "") or "")
    details = {
        "session_id": state.sid,
        "routing_mode": getattr(state.sess, "routing_mode", "auto"),
        "selected_agent": state.selected_agent,
        "termination_reason": termination,
    }
    if termination == "context_window_exceeded" and not state.answer_text:
        return ErrorInfo(
            error="context_window_exceeded",
            message=CONTEXT_FULL_MESSAGE,
            details=details,
            recoverable=True,
        )
    if (
        not state.answer_text
        and not state.thinking_text
        and not state.nanoagents
        and not has_operation_output
        and getattr(pred, "assignment_disposition", None) != "yield"
    ):
        return ErrorInfo(
            error="empty_response",
            message="Agent completed without user-visible output.",
            details=details,
            recoverable=True,
        )
    return None
