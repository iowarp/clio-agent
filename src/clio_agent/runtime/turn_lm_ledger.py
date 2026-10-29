"""The LM calls a turn made: the only place a call's DSPy history entry lives.

clio-core holds the record of every model call (the ``lm.call`` event); the agent
process keeps none. DSPy, left alone, appends each call's entry -- its whole prompt,
response, tool schemas and usage -- to a process-wide history, to the LM's own history
and to the history of every module on the call stack, and keeps them for as long as
those objects live. In a multi-agent run that is every child session's agent modules
and LMs: the process grew by gigabytes over a deep-research run and never gave it back.

Every clio LM carries the ``lm.call`` callback, which routes the LM's history here
(:func:`route_history`): an entry joins the open turn's ledger and nothing else, and
the ledger ends with the turn. The turn reads its usage, reasoning and prompt size
from :func:`ledger_calls` (``gact.usage``).

The turn opens a ledger (:func:`open_ledger`) before its forward. It is held in a
``ContextVar``: the forward runs in executor threads under a
``contextvars.copy_context()`` of the turn, and every LM call runs on the LM loop in a
copy of its caller's context (``lm.engines.lm_loop``); a copied context shares the SAME
list object, so a call made in any of them lands in the turn's ledger. A child turn
opens its own ledger in its own context, so a parent never counts a child's calls. A
call outside a turn (the CLI, the optimizer) keeps no entry.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "close_ledger",
    "last_call",
    "ledger_calls",
    "open_ledger",
    "record_call",
    "route_history",
    "take_adapter_entry",
]

_LEDGER: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "clio_turn_lm_ledger", default=None
)

# An adapter call's entry, from the moment DSPy records it until the ``lm.call``
# handler that ends the same call takes it (``take_adapter_entry``): the record needs
# the raw response behind the call's list outputs, in a turn or not.
_HANDOFF_LOCK = threading.Lock()
_HANDOFF: dict[int, dict[str, Any]] = {}
#: Adapter calls in flight at once never come near this; past it, the oldest go.
_HANDOFF_CAP = 256


def open_ledger() -> contextvars.Token[list[dict[str, Any]] | None]:
    """Start a fresh ledger for the current turn's context.

    Returns:
        The token that :func:`close_ledger` takes to restore the previous ledger.
    """

    return _LEDGER.set([])


def close_ledger(token: contextvars.Token[list[dict[str, Any]] | None]) -> None:
    """Restore the ledger that was active before :func:`open_ledger`."""

    _LEDGER.reset(token)


def record_call(entry: dict[str, Any]) -> None:
    """Keep one call's DSPy history entry in the open turn's ledger; none outside a turn.

    Installed as an LM's ``update_history`` by :func:`route_history`, so DSPy hands
    each finished call's entry here instead of to its own histories.
    """

    if not isinstance(entry, dict):
        return
    if isinstance(entry.get("outputs"), list) and "request" not in entry:
        # An adapter call (a typed call records its Request): its end handler takes it.
        with _HANDOFF_LOCK:
            _HANDOFF[id(entry["outputs"])] = entry
            if len(_HANDOFF) > _HANDOFF_CAP:
                _HANDOFF.pop(next(iter(_HANDOFF)))
                logger.warning(
                    "LM adapter entry dropped unclaimed reason=lm_adapter_handoff_overflow cap=%d",
                    _HANDOFF_CAP,
                )
    ledger = _LEDGER.get()
    if ledger is not None:
        ledger.append(entry)


def take_adapter_entry(outputs: Any) -> dict[str, Any] | None:
    """The entry DSPy recorded for an adapter call's list ``outputs``, handed over once.

    The identity of the list matches exactly even when other threads share the LM.
    """

    if not isinstance(outputs, list):
        return None
    with _HANDOFF_LOCK:
        entry = _HANDOFF.pop(id(outputs), None)
    return entry if entry is not None and entry.get("outputs") is outputs else None


def route_history(lm: Any) -> None:
    """Send ``lm``'s history entries to the turn ledger instead of DSPy's histories.

    DSPy records a call through ``lm.update_history(entry)``; an instance attribute
    takes the place of the method, and ``lm.copy()`` (a session's view over its pool)
    carries it along.
    """

    if getattr(lm, "update_history", None) is not record_call:
        lm.update_history = record_call


def ledger_calls() -> list[dict[str, Any]]:
    """The entries of the calls made so far in the current turn (empty outside a turn)."""

    return list(_LEDGER.get() or ())


def last_call() -> dict[str, Any] | None:
    """The current turn's newest call entry, or ``None``."""

    ledger = _LEDGER.get()
    return ledger[-1] if ledger else None
