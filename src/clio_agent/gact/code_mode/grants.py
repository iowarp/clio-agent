"""A ``run_code`` script's approval, and how it covers the script's own tool calls.

Running a script is one non-read call, approved before it runs like ``shell_bash``. The
script's nested calls still go through the permission gate with their own annotations:
reads never ask, deny policies and plan mode still deny. For a nested non-read call that
would ask, the approval mode decides (the plan's table):

- ``bypass``: the call runs (the gate already allows it);
- ``auto-edits``, ``ai-review``, ``spotter-ai``, ``spotter-watcher`` (the default
  ``code.covering_postures``): the script's approval covers it;
- ``ask``: a person who approved the script approved the calls it names: a nested call
  to a tool the script's code names (``tools.<group>.<tool>`` or ``tools.call("<name>")``)
  is covered. A call the code does not name (one found with ``tools.search`` and called
  through a variable) asks, one question per script at a time, showing the script; the
  answer "Allow for the rest of this script" (``allow_script``) covers the rest of that
  call.

The question that approves the script itself offers ``allow_script`` too: choosing it
there covers all the script's nested calls from the start, named or not. "Allow once"
there covers the calls the script names, as above. An approval that came from a rule or
the reviewer rather than a person covers nothing under ``ask``.

A kernel binding the script uses (``browser.open(url)``) is part of the script's code: the
tools the binding's own source names are named by a script that references the binding,
so the person who approved ``await browser.open(url)`` approved the ``browser_open`` call
it makes. "Allow once" on a nested question covers the same subject (the same tool with
the same arguments, the same browser host) for the rest of that script, never another
subject; a rule set to "Ask every time" still asks every time.

The grant lives in a ``ContextVar`` that ``run_code`` binds for its nested calls, so
the gate (which runs in the nested call's thread) sees it, and nothing outside the
script does. Nothing bounds a script's time, so waiting for an answer costs it nothing
but the wait.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from typing import Any

from clio_agent import conf

__all__ = [
    "ALLOW_SCRIPT",
    "REASON_SCRIPT_GRANT",
    "ScriptGrant",
    "active_grant",
    "allow_once",
    "allowed_once",
    "approval_mode",
    "approving",
    "bind_grant",
    "binding_tools",
    "covering_postures",
    "covers",
    "grant_in",
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
    #: A person answered the script's own question with an allow: the calls its code
    #: names are covered under ``ask``.
    approved: bool = False
    #: The agent's MCP server names, which split a tool name into ``<group>.<tool>``.
    servers: tuple[str, ...] = ()
    #: One open question per script: other calls that need an answer wait behind it.
    asking: threading.Lock = field(default_factory=threading.Lock)
    #: The session's approval mode when the script was approved: its nested calls keep
    #: it, so changing the mode while a background script runs does not change what the
    #: script may do (CK10). Empty: the session's current mode applies.
    approval_mode: str = ""
    #: Tools the kernel bindings this script references name in their own source.
    bound_tools: frozenset[str] = frozenset()
    #: Subjects a person allowed once during this script (:func:`subject_key`).
    allowed: set[str] = field(default_factory=set)

    def names(self, tool: str) -> bool:
        """Whether the script's code names ``tool`` as one of its calls."""
        from clio_agent.gact.code_mode.stubs import tool_group  # noqa: PLC0415

        if tool in self.bound_tools:
            return True
        if tool in {m.group(2) for m in _CALL_BY_NAME.finditer(self.code)}:
            return True
        group, function = tool_group(tool, self.servers)
        wanted = (group, function) if group else (function, "")
        return wanted in {(m.group(1), m.group(2) or "") for m in _ATTRIBUTE.finditer(self.code)}


#: ``tools.<group>.<tool>`` or ``tools.<tool>`` in a script's code.
_ATTRIBUTE = re.compile(r"\btools\s*\.\s*([A-Za-z_]\w*)(?:\s*\.\s*([A-Za-z_]\w*))?")
#: ``tools.call("<name>"`` in a script's code.
_CALL_BY_NAME = re.compile(r"\btools\s*\.\s*call\s*\(\s*(['\"])([^'\"\n]+)\1")


def binding_tools(
    code: str, bindings: Mapping[str, Mapping[str, Any]], tool_names: Iterable[str]
) -> frozenset[str]:
    """The tools named by the kernel bindings ``code`` references, from their own source.

    A binding is the script's library: a script that writes ``browser.open(url)`` calls the
    ``browser_open`` the binding's source names. A name the source holds as a string
    constant names a tool when it is one of ``tool_names``.
    """
    known = frozenset(tool_names)
    found: set[str] = set()
    for name, binding in bindings.items():
        if not re.search(rf"\b{re.escape(str(name))}\b", code):
            continue
        try:
            tree = ast.parse(str(binding.get("source") or ""))
        except SyntaxError:
            continue  # the kernel refuses it too; it names nothing
        found.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            if node.value in known
        )
    return frozenset(found)


def subject_key(kind: str, subject: Any) -> str:
    """One subject a person can allow once (``("tool", [name, args])``, ``("browser_host",
    host)``), as a stable key."""
    return json.dumps([kind, subject], sort_keys=True, default=str)


_ACTIVE: ContextVar[ScriptGrant | None] = ContextVar("clio_run_code_grant", default=None)
#: The grant of a script whose own approval is being asked: it only shapes that question
#: and takes its answer, never decides a call (the script is not running yet).
_APPROVING: ContextVar[ScriptGrant | None] = ContextVar("clio_run_code_approving", default=None)


@contextlib.contextmanager
def approving(grant: ScriptGrant) -> Iterator[ScriptGrant]:
    """Ask the script's own approval with ``allow_script`` offered for ``grant``."""
    token = _APPROVING.set(grant)
    try:
        yield grant
    finally:
        _APPROVING.reset(token)


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


def grant_in(context: Context) -> ScriptGrant | None:
    """The grant of the script whose call captured ``context`` (without entering it)."""
    return context.get(_ACTIVE)


def allow_once(kind: str, subject: Any, grant: ScriptGrant | None = None) -> None:
    """A person allowed ``subject`` once: it is allowed for the rest of the script."""
    grant = grant if grant is not None else _ACTIVE.get()
    if grant is not None:
        grant.allowed.add(subject_key(kind, subject))


def allowed_once(kind: str, subject: Any, grant: ScriptGrant | None = None) -> bool:
    """Whether a person already allowed ``subject`` once during this script."""
    grant = grant if grant is not None else _ACTIVE.get()
    return grant is not None and subject_key(kind, subject) in grant.allowed


def bind_grant(grant: ScriptGrant | None) -> Any:
    """Bind ``grant`` for the calls made in this context; returns the reset token."""
    return _ACTIVE.set(grant)


def reset_grant(token: Any) -> None:
    """Undo :func:`bind_grant`."""
    _ACTIVE.reset(token)


def covers(posture: str, tool: str = "", args: Any = None) -> bool:
    """Whether a nested call to ``tool`` that would ask runs under the script's approval."""
    grant = _ACTIVE.get()
    if grant is None:
        return False
    if grant.covered or posture in covering_postures():
        return True
    if tool and args is not None and allowed_once("tool", [tool, args], grant):
        return True
    return grant.approved and bool(tool) and grant.names(tool)


def row_fields(actions: list[str]) -> dict[str, Any]:
    """What a permission question asked from inside a script adds to its row."""
    grant = _ACTIVE.get() or _APPROVING.get()
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
        yield


def answered(row: Mapping[str, Any], tool: str = "", args: Any = None) -> None:
    """An ``allow_script`` answer covers the rest of the script (all of it, when the
    answer approved the script itself); a person's other allow of the script itself
    covers the calls its code names; a person's allow of a nested call to ``tool`` with
    ``args`` covers that same call for the rest of the script."""
    from clio_agent.gact.runtime.grants import GRANTOR_USER  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_actions import allows  # noqa: PLC0415

    active = _ACTIVE.get()
    grant = active or _APPROVING.get()
    if grant is None:
        return
    if row.get("action") == ALLOW_SCRIPT:
        grant.covered = True
    elif row.get("grantor") == GRANTOR_USER and allows(row.get("action")):
        if active is None:
            grant.approved = True
        elif tool and args is not None:
            allow_once("tool", [tool, args], active)
