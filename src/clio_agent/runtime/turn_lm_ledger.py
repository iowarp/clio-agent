"""The LMs a turn constructed, so its usage rollup can read their history.

A turn's token and cost rollup (``gact.turn_usage`` / ``gact.usage``) sums the
history of every LM the turn called. The long-lived LMs (the boot default, the
agent's ``_main_lm``) are reachable from the app, but an expert or dynamic
agent builds a FRESH LM for each forward from its resolved provider spec
(``create_hooked_lm(cfg)`` inside ``dspy.context``). Those per-forward LMs were
invisible to the rollup, so a turn that ran entirely on one of them (ALCF Metis,
Codex, any per-message model) recorded zero tokens.

The turn opens a ledger (:func:`open_ledger`) before its forward; every LM
built while it is open (:func:`record`, called from ``lm.factory.create_lm``)
joins it; the rollup reads :func:`ledger_lms`. The ledger is a list held in a
``ContextVar``: the forward runs in executor threads under a
``contextvars.copy_context()`` of the turn, and a copied context shares the SAME
list object, so an LM built in a worker thread is visible to the turn coroutine.
A child turn opens its own ledger in its own context, so a parent never counts a
child's calls twice.
"""

from __future__ import annotations

import contextvars
from typing import Any

__all__ = ["close_ledger", "ledger_lms", "open_ledger", "record"]

_LEDGER: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar(
    "clio_turn_lm_ledger", default=None
)


def open_ledger() -> contextvars.Token[list[Any] | None]:
    """Start a fresh ledger for the current turn's context.

    Returns:
        The token that :func:`close_ledger` takes to restore the previous ledger.
    """

    return _LEDGER.set([])


def close_ledger(token: contextvars.Token[list[Any] | None]) -> None:
    """Restore the ledger that was active before :func:`open_ledger`."""

    _LEDGER.reset(token)


def record(lm: Any) -> None:
    """Add ``lm`` to the open ledger; a no-op outside a turn (CLI, optimizer)."""

    ledger = _LEDGER.get()
    if ledger is not None and not any(entry is lm for entry in ledger):
        ledger.append(lm)


def ledger_lms() -> list[Any]:
    """The LMs built so far in the current turn (empty outside a turn)."""

    return list(_LEDGER.get() or ())
