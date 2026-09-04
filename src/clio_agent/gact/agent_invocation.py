"""Compatibility-aware registered-agent invocation."""

from __future__ import annotations

import inspect
from typing import Any


def select_accepted_kwargs(
    func: Any, candidate: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return ``(accepted kwargs, dropped names)`` for one compat call.

    Signature inspection replaces the old TypeError-message sniffing: we decide
    which optional kwargs a callee understands *before* invoking it, so the call
    happens exactly once and any ``TypeError`` raised from inside the callee
    propagates as-is rather than being mistaken for a signature mismatch.

    The selection is ``None`` when ``func`` cannot be introspected (some C-level /
    builtin callables raise ``ValueError``/``TypeError`` from
    :func:`inspect.signature`); the caller then makes a single best-effort
    attempt with the full candidate set instead of guessing.

    The dropped names are returned rather than discarded so a caller can decide
    whether a drop is benign (an optional mode flag an older signature predates)
    or a real degradation (a MODEL INPUT — images/files — that silently never
    reaches the model). Callers that pass model inputs must record a typed
    reason for a non-empty drop; a silent one is the defect this return value
    exists to prevent.
    """

    try:
        signature = inspect.signature(func)
    except (ValueError, TypeError):
        return None, []
    parameters = signature.parameters.values()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return dict(candidate), []
    accepted = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    selected = {name: value for name, value in candidate.items() if name in accepted}
    return selected, sorted(set(candidate) - accepted)


def _select_accepted_kwargs(func: Any, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Return accepted kwargs, or ``None`` when the callable cannot be inspected."""

    selected, _dropped = select_accepted_kwargs(func, candidate)
    return selected


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


__all__ = ["_run_dynamic_agent_compat", "select_accepted_kwargs"]
