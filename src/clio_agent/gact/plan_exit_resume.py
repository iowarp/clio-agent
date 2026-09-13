"""Model context for resuming after a human plan-exit decision."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_CONSTRAINT_LIFT_HEADER = "[STATE TRANSITION OVERRIDE]"


def approved_plan_resume_text(
    decision: str, plan_file: str, approved_plan: Mapping[str, Any]
) -> str:
    """Compose the constraint-lifting context for an approved plan exit."""

    base = (
        f"{_CONSTRAINT_LIFT_HEADER} Your plan at {plan_file} has been APPROVED. The previous "
        "read-only / plan-mode constraints are now LIFTED — you are authorized to modify files to "
        "implement the approved plan."
    )
    instruction = (
        " Begin implementing it; you will be prompted to approve each action."
        if decision == "interactive"
        else " Begin implementing the approved plan now."
    )
    content = str(approved_plan.get("content") or "")
    safe_content = content.replace("</approved-plan>", "[closing tag removed]")
    context_metadata = {key: value for key, value in approved_plan.items() if key != "content"}
    return (
        base
        + instruction
        + "\n\nApproved plan context: "
        + json.dumps(context_metadata, ensure_ascii=False, sort_keys=True, default=str)
        + "\n<approved-plan>\n"
        + safe_content
        + "</approved-plan>"
    )


def rejected_plan_resume_text(feedback: str, plan_file: str) -> str:
    """Compose revision context for a rejected plan exit."""

    note = feedback or "(no additional feedback provided)"
    return (
        "Your request to exit plan mode was REJECTED — you are STILL in plan mode. Revise the plan "
        f"at {plan_file} per the reviewer's feedback, then call plan_exit again.\n\n"
        f"Reviewer feedback: {note}"
    )
