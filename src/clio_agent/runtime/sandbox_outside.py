"""Running one shell command outside CLIO's sandbox, after the person allows that command.

The fence (:mod:`clio_agent.runtime.sandbox`) confines every agent-spawned command to its
write roots, and a folder grant widens them. Some blocks no folder grant lifts: the
sandboxed child runs without the server account's privileges, so a server running as root
still cannot replace files another account owns inside a granted folder (a package
manager's download folders), and a system install never succeeds there. For those, the
agent asks to run that one command outside the sandbox (``shell_bash`` with
``outside_sandbox``): the permission gate always asks the person, whatever the approval
mode or the AI reviewer would do, and shows the exact command and the agent's reason.

A person's allow is recorded here for that exact command; the shell takes it once, within
:data:`APPROVAL_TTL_S`, and only then runs the command unfenced. A command that reached the
shell without a recorded allow is refused (``outside_sandbox_not_approved``), so a path
that skipped the gate fails closed. CLIO never takes a password: a command that needs one
(``sudo``) is still the person's to run in their own terminal.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Mapping
from typing import Any

#: The ``shell_bash`` argument that asks for this.
ARGUMENT = "outside_sandbox"
#: Typed refusal when the shell gets the argument without a person's allow.
REASON_NOT_APPROVED = "outside_sandbox_not_approved"
#: How long a person's allow waits for its command to start.
APPROVAL_TTL_S = 600.0

_LOCK = threading.Lock()
_APPROVED: dict[str, float] = {}


def requested(name: str, args: Mapping[str, Any]) -> bool:
    """Whether this tool call asks to run outside the sandbox."""
    return name == "shell_bash" and args.get(ARGUMENT) is True


def _key(command: str) -> str:
    return hashlib.sha256(command.strip().encode("utf-8")).hexdigest()


def approve(command: str) -> None:
    """Record a person's allow for exactly this command (the gate calls this)."""
    now = time.monotonic()
    with _LOCK:
        for key, at in list(_APPROVED.items()):
            if now - at > APPROVAL_TTL_S:
                del _APPROVED[key]
        _APPROVED[_key(command)] = now


def take(command: str) -> bool:
    """Use up the allow for exactly this command; ``False`` when there is none."""
    with _LOCK:
        at = _APPROVED.pop(_key(command), None)
    return at is not None and time.monotonic() - at <= APPROVAL_TTL_S


def card_summary(args: Mapping[str, Any]) -> str:
    """The permission card's title for an outside-the-sandbox command."""
    reason = str(args.get("access_reason") or "").strip()
    head = "Run this command outside CLIO's sandbox?"
    return f"{head} {reason}"[:500] if reason else head
