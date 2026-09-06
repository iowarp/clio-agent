"""Typed Plan-exit projection helpers for the normalized interaction ledger."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.types import UserQuestion

_PLAN_REVIEW_KEYS = (
    "summary",
    "recommended_mode",
    "risk_notes",
    "plan_file",
    "plan_content",
    "plan_content_status",
    "plan_content_error",
    "plan_content_chars",
    "plan_content_included_chars",
)


def is_plan_exit_question(question: UserQuestion) -> bool:
    """Return whether a question owns the native Plan-exit lifecycle."""

    return bool(question.source == "plan_exit" or question.metadata.get("plan_exit_approval"))


def plan_exit_payload(question: UserQuestion) -> dict[str, Any]:
    """Project the bounded saved-plan review fields carried by the question."""

    return {
        key: question.metadata.get(key)
        for key in _PLAN_REVIEW_KEYS
        if question.metadata.get(key) not in ("", None)
    }


__all__ = ["is_plan_exit_question", "plan_exit_payload"]
