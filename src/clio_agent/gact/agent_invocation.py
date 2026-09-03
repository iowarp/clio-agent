"""Compatibility-aware registered-agent invocation."""

from __future__ import annotations

import inspect
from typing import Any


def _callable_positional_slots(func: Any, count: int) -> bool:
    """Return whether ``func`` accepts at least ``count`` positional arguments."""

    try:
        signature = inspect.signature(func)
    except (ValueError, TypeError):
        return True
    slots = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            slots += 1
    return slots >= count


def _run_dynamic_agent_compat(
    runner: Any,
    base_agent: Any,
    dynamic_agent: Any,
    question: str,
    sid: str,
    cancel_requested: Any | None,
    images: list[Any] | None = None,
    files: list[Any] | None = None,
) -> Any:
    """Call one dynamic-agent runner exactly once with its accepted arguments."""

    args: list[Any] = [base_agent, dynamic_agent, question, sid]
    if _callable_positional_slots(runner, 5):
        args.append(cancel_requested)
    if _callable_positional_slots(runner, 6):
        args.append(list(images or []))
    if _callable_positional_slots(runner, 7):
        args.append(list(files or []))
    return runner(*args)


__all__ = ["_run_dynamic_agent_compat"]
