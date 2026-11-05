"""A ``run_code`` script's approval, and how it covers the script's own tool calls.

Running a script is one non-read call, approved before it runs like ``shell_bash``. The
script's nested calls still go through the permission gate with their own annotations:
reads never ask, deny policies and plan mode still deny. For a nested non-read call that
would ask, the approval mode decides (the plan's table):

- ``bypass``: the call runs (the gate already allows it);
- ``auto-edits``, ``ai-review``, ``spotter-ai``, ``spotter-watcher`` (the default
  ``code.covering_postures``): the script's approval covers it;
- ``ask``: it asks, one question per script at a time, showing the script; the answer
  "Allow for the rest of this script" (``allow_script``) covers the rest of that call.

The grant lives in a ``ContextVar`` that ``run_code`` binds for its nested calls, so
the gate (which runs in the nested call's thread) sees it, and nothing outside the
script does. Nothing bounds a script's time, so waiting for an answer costs it nothing
but the wait.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from clio_agent import conf

__all__ = [
    "ALLOW_SCRIPT",
    "REASON_SCRIPT_GRANT",
    "ScriptGrant",
    "active_grant",
    "approval_mode",
    "bind_grant",
    "covering_postures",
    "covers",
    "max_script_chars",
    "question",
    "reset_grant",
    "row_fields",
]

#: The answer that covers the rest of one script's calls.
ALLOW_SCRIPT = "allow_script"
#: Typed audit reason for a nested call the script's approval covered.
REASON_SCRIPT_GRANT = "run_code_script_grant"
#: The default ``code.covering_postures``.
_COVERING_POSTURES: Sequence[str] = ("auto-edits", "ai-review", "spotter-ai", "spotter-watcher")


def covering_postures() -> frozenset[str]:
    """Approval postures in which the script's approval covers its nested non-read calls."""
    return frozenset(
        conf.resolve(
            "code.covering_postures",
            env="CLIO_CODE_COVERING_POSTURES",
            default=_COVERING_POSTURES,
            cast=conf.as_csv,
        )
    )


def max_script_chars() -> int:
    """How much of the script a permission question carries (``code.max_script_chars``).

    Raises:
        ValueError: The configured length is below 1.
    """
    value = conf.resolve(
        "code.max_script_chars", env="CLIO_CODE_MAX_SCRIPT_CHARS", default=20_000, cast=conf.as_int
    )
    if value < 1:
        raise ValueError(f"code.max_script_chars must be at least 1, not {value}")
    return value


@dataclass
class ScriptGrant:
    """One ``run_code`` call's approval and the state of its open question."""

    call_id: str
    code: str
    #: "Allow for the rest of this script" was chosen.
    covered: bool = False
    #: One open question per script: other calls that need an answer wait behind it.
    asking: threading.Lock = field(default_factory=threading.Lock)
    #: A question is open now (the row says "Script paused, waiting for you").
    waiting: bool = False
    #: The session's approval mode when the script was approved: its nested calls keep
    #: it, so changing the mode while a background script runs does not change what the
    #: script may do (CK10). Empty: the session's current mode applies.
    approval_mode: str = ""


_ACTIVE: ContextVar[ScriptGrant | None] = ContextVar("clio_run_code_grant", default=None)


def approval_mode(current: Any) -> str:
    """The approval mode a call is decided under: its script's, when it is a script's
    nested call, else the session's (``ask`` when there is no session)."""
    grant = _ACTIVE.get()
    if grant is not None and grant.approval_mode:
        return grant.approval_mode
    return str(getattr(current, "approval_mode", "ask") or "ask") if current is not None else "ask"


def active_grant() -> ScriptGrant | None:
    """The grant of the script whose nested call is running here, if any."""
    return _ACTIVE.get()


def bind_grant(grant: ScriptGrant | None) -> Any:
    """Bind ``grant`` for the calls made in this context; returns the reset token."""
    return _ACTIVE.set(grant)


def reset_grant(token: Any) -> None:
    """Undo :func:`bind_grant`."""
    _ACTIVE.reset(token)


def covers(posture: str) -> bool:
    """Whether a nested call that would ask runs under the script's approval."""
    grant = _ACTIVE.get()
    return grant is not None and (grant.covered or posture in covering_postures())


def row_fields(actions: list[str]) -> dict[str, Any]:
    """What a permission question asked from inside a script adds to its row."""
    grant = _ACTIVE.get()
    if grant is None:
        return {}
    return {
        "script": {"call_id": grant.call_id, "code": grant.code[: max_script_chars()]},
        "actions": [*actions, ALLOW_SCRIPT],
    }


@contextlib.contextmanager
def question() -> Iterator[None]:
    """Hold the script's one open question; the time waited pauses its clock."""
    grant = _ACTIVE.get()
    if grant is None:
        yield
        return
    with grant.asking:
        grant.waiting = True
        try:
            yield
        finally:
            grant.waiting = False


def answered(row: Mapping[str, Any]) -> None:
    """An ``allow_script`` answer covers the rest of the script."""
    grant = _ACTIVE.get()
    if grant is not None and row.get("action") == ALLOW_SCRIPT:
        grant.covered = True
