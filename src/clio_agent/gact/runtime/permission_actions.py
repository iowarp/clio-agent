"""The answers a permission question can get and the actions a policy can carry, defined once.

A person answers a question with one of :data:`ANSWERS`. :data:`STANDARD_ANSWERS` are
offered on every question; a question may offer more in its row's ``actions`` (a
``run_code`` script's question adds ``allow_script``, "Allow for the rest of this
script"), and an answer outside the standard set is valid only when its row offers it.
A stored policy carries one of :data:`POLICY_ACTIONS`; ``allow_script`` is never a
policy, since it lasts for one script.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "ALLOWING",
    "ALLOW_SCRIPT",
    "ANSWERS",
    "POLICY_ACTIONS",
    "POLICY_ALLOWING",
    "STANDARD_ANSWERS",
    "allows",
    "offered",
]

ALLOW_SCRIPT = "allow_script"
#: Offered on every question, in this order.
STANDARD_ANSWERS: tuple[str, ...] = ("allow", "deny", "allow_session", "allow_workspace")
#: Stored policy actions that let a call run.
POLICY_ALLOWING = frozenset({"allow", "allow_session", "allow_workspace"})
#: Every action a stored policy may carry.
POLICY_ACTIONS = POLICY_ALLOWING | {"deny", "ask"}
#: Answers that let the call run.
ALLOWING = POLICY_ALLOWING | {ALLOW_SCRIPT}
#: Every answer a question can get.
ANSWERS = ALLOWING | {"deny"}


def allows(action: object) -> bool:
    """Whether an answer lets the call run."""
    return action in ALLOWING


def offered(row: Mapping[str, Any], action: str) -> bool:
    """Whether ``action`` is a valid answer to the question in ``row``."""
    if action not in ANSWERS:
        return False
    extra = row.get("actions")
    if action in STANDARD_ANSWERS:
        return row.get("kind") != "folder_access" or action in (extra or ())
    return action in (extra or ())
