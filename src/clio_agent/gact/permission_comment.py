"""A person's comment on a permission answer, carried to the agent with the decision.

The approval card lets the person write a comment with either answer. With Allow it is a
note the agent reads beside the tool's result; with Deny it is the reason the agent reads
in place of the generic refusal. The comment is the person's own words, kept on the
permission row (``comment``) and passed on unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.runtime.permission_actions import allows


class AllowDecision(str):
    """A gate ``"allow"`` that carries the person's comment as :attr:`note` for the agent.

    It IS ``"allow"``, so every ``decision == "allow"`` check holds; the execution
    boundary reads :attr:`note` (via ``getattr``) and puts it before the tool's result.
    """

    note: str

    def __new__(cls, note: str = "") -> "AllowDecision":
        obj = super().__new__(cls, "allow")
        obj.note = note
        return obj


def person_comment(text: Any) -> str:
    """The comment a person wrote with their answer, or ``""``."""
    return str(text or "").strip()


def person_decision(row: Mapping[str, Any]) -> str:
    """The gate decision for a person's answer, carrying their comment when they wrote one.

    The agent reads the action as the person's card named it ("Run command"), the same
    title the card showed, so a refusal is never phrased with a tool id.
    """
    from clio_agent.gact.permission_delivery import permission_title  # noqa: PLC0415
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415

    action, comment = str(row.get("action") or "deny"), person_comment(row.get("comment"))
    title = permission_title(row) or "this action"
    if allows(action):
        return (
            AllowDecision(f'The person allowed "{title}" and commented: {comment}')
            if comment
            else "allow"
        )
    said = f" and said: {comment}" if comment else ""
    return DenyDecision(f'The person declined "{title}"{said}')
