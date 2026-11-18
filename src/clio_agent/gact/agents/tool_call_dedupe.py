"""Identical tool calls in one model reply run once.

A model can repeat the exact same call (same tool, same arguments) many times in
one reply, for example 561 identical ``search_tools`` loads. Running every copy
changes nothing but the context size, so the loop runs each distinct call once and
tells the model so in that call's result. This is format-level repair of the reply,
not a decision about the work: distinct calls all run, in order. Each collapse is
recorded as the typed degradation :data:`TOOL_CALLS_COLLAPSED`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

#: Typed degradation reason (``gact.runtime.capabilities`` catalog).
TOOL_CALLS_COLLAPSED = "tool_calls_collapsed"


def _identity(call: Any) -> str:
    return call.name + "\x00" + json.dumps(call.input, sort_keys=True, default=str)


def collapse_identical(calls: Sequence[Any]) -> tuple[list[Any], dict[str, int]]:
    """The distinct calls in reply order, and how many extra copies each kept call had."""

    kept: list[Any] = []
    first: dict[str, Any] = {}
    repeats: dict[str, int] = {}
    for call in calls:
        key = _identity(call)
        if key in first:
            repeats[first[key].id] = repeats.get(first[key].id, 0) + 1
            continue
        first[key] = call
        kept.append(call)
    return kept, repeats


def repeat_note(name: str, extra: int) -> str:
    """The sentence the model reads with the one result its repeated call produced."""

    copies = "time" if extra == 1 else "times"
    return (
        f"\n\n[clio: your last reply repeated this exact {name} call {extra} more {copies}; "
        "it ran once. Make each call once.]"
    )


def with_repeat_notes(
    calls: Sequence[Any], outcomes: list[Any], repeats: dict[str, int]
) -> list[Any]:
    """``outcomes`` with the repeat note appended to each collapsed call's text result."""

    if not repeats:
        return outcomes
    noted = []
    for call, outcome in zip(calls, outcomes, strict=True):
        extra = repeats.get(call.id, 0)
        if extra and isinstance(outcome.value, str):
            outcome = dataclasses.replace(
                outcome, value=outcome.value + repeat_note(call.name, extra)
            )
        noted.append(outcome)
    return noted


def record_collapsed(
    app: Any, session_id: str, calls: Sequence[Any], repeats: dict[str, int]
) -> None:
    """Record the collapse as a typed per-session degradation (no silent repair)."""

    if not repeats:
        return
    from clio_agent.gact.stream_fallbacks import record_stream_fallback_note  # noqa: PLC0415

    names = {call.id: call.name for call in calls}
    message = "; ".join(
        f"{names.get(call_id, '?')} x{extra + 1} ran once" for call_id, extra in repeats.items()
    )
    record_stream_fallback_note(app, session_id, TOOL_CALLS_COLLAPSED, message)


__all__ = [
    "TOOL_CALLS_COLLAPSED",
    "collapse_identical",
    "record_collapsed",
    "repeat_note",
    "with_repeat_notes",
]
