"""The ``hook`` decorator.

A plugin's ``ai.iowarp.clio/hooks/*.py`` declares hooks as decorated functions::

    @hook("tool_result", after="core.audit")
    def redact_paths(ev): ...

Every hook has an id, ``<plugin>.<function name>`` unless ``id=`` says
otherwise, so ``before=`` and ``after=`` can reference it. Resolution orders
hooks from child to parent (the plugins a subclass or a user delta adds run
before the ones its parent carried), applies ``before``/``after``, and rejects a
cycle.

A hook receives a :class:`HookEvent` and returns ``None`` to allow, a mapping
with ``decision`` (``allow``/``deny``), ``reason`` and ``additional_context``, or
raises :class:`PermissionError` to deny with that message.

An ``answer`` hook sees the turn's final answer in ``ev.answer`` and may return
``note`` (a line shown under the answer), ``check`` (``{"passed": bool, "text":
str}``) and ``answer`` (a replacement, shown as a revision with ``reason``)::

    @hook("answer")
    def cite_check(ev):
        if "http" not in (ev.answer or ""):
            return {"check": {"passed": False, "text": "No source is cited."}}
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: SDK event names and the CLIO hook events they fire on.
HOOK_EVENTS: dict[str, str] = {
    "tool_call": "PreToolUse",
    "tool_result": "PostToolUse",
    "tool_batch": "PostToolBatch",
    "prompt_submit": "UserPromptSubmit",
    "stop": "Stop",
    "answer": "Answer",
    "session_start": "SessionStart",
    "session_end": "SessionEnd",
    "subagent_start": "SubagentStart",
    "subagent_stop": "SubagentStop",
    "pre_compact": "PreCompact",
    "before_model": "BeforeModel",
    "after_model": "AfterModel",
}


@dataclass(frozen=True)
class HookEvent:
    """What a hook sees for one event."""

    event: str
    session_id: str = ""
    turn_id: str = ""
    cwd: str = ""
    tool_name: str | None = None
    tool_input: Mapping[str, Any] | None = None
    prompt: str | None = None
    payload: Mapping[str, Any] | None = None
    answer: str | None = None


@dataclass(frozen=True)
class HookSpec:
    """A hook declared with :func:`hook`."""

    event: str
    function: Callable[[HookEvent], Any]
    id: str = ""
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    tools: str = ""
    fail_closed: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __call__(self, ev: HookEvent) -> Any:
        return self.function(ev)


def _names(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def hook(
    event: str,
    *,
    id: str = "",  # noqa: A002 - the design's public field name
    before: str | list[str] | tuple[str, ...] | None = None,
    after: str | list[str] | tuple[str, ...] | None = None,
    tools: str = "",
    fail_closed: bool = False,
) -> Callable[[Callable[[HookEvent], Any]], HookSpec]:
    """Declare a hook on ``event`` (one of :data:`HOOK_EVENTS`).

    ``tools`` is an anchored regular expression over tool names for the tool
    events. ``fail_closed`` makes an exception inside a deny-capable hook deny
    the call instead of letting it through.
    """

    if event not in HOOK_EVENTS:
        known = ", ".join(sorted(HOOK_EVENTS))
        raise ValueError(f"unknown hook event {event!r}; expected one of: {known}")

    def wrap(function: Callable[[HookEvent], Any]) -> HookSpec:
        return HookSpec(
            event=event,
            function=function,
            id=id or function.__name__,
            before=_names(before),
            after=_names(after),
            tools=tools,
            fail_closed=fail_closed,
        )

    return wrap
