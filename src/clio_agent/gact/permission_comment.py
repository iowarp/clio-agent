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
    """The gate decision for a person's answer, carrying their comment when they wrote one."""
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415

    action, comment = str(row.get("action") or "deny"), person_comment(row.get("comment"))
    name = str((row.get("tool_call") or {}).get("tool_name") or "this call")
    if allows(action):
        return (
            AllowDecision(f"The person allowed {name} and commented: {comment}")
            if comment
            else "allow"
        )
    if not comment:
        return "deny"
    return DenyDecision(f"The person denied {name} and said: {comment}")
