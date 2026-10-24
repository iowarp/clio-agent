"""A turn LM ledger for tests: the calls a turn made, without running a turn."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from clio_agent.runtime import turn_lm_ledger


@contextmanager
def turn_with_calls(calls: list[dict[str, Any]] = ()) -> Iterator[None]:  # type: ignore[assignment]
    """A turn whose LM ledger already holds ``calls`` (DSPy history entries)."""
    token = turn_lm_ledger.open_ledger()
    try:
        for entry in calls:
            turn_lm_ledger.record_call(entry)
        yield
    finally:
        turn_lm_ledger.close_ledger(token)
