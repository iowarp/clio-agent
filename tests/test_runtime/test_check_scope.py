"""One access decision reads each of its inputs once; the next decision reads afresh."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.runtime import check_scope, storage_access


def test_a_decision_reads_the_ledger_once_and_the_next_sees_a_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside one decision the ledger and the roots derived from it are read once; a
    change made between two decisions is seen by the second, and with no decision open
    every read is fresh.

    SABOTAGE: keep ``once``'s memo for the process (a module dict instead of the scope's)
    -> the second decision sees the old ledger -> red. Drop the ``once`` in
    ``_source_records`` -> one decision reads the ledger three times -> red."""
    ledger: list[list[dict[str, Any]]] = [[{"source": {"id": "a"}}]]
    reads: list[int] = []

    def read() -> list[dict[str, Any]]:
        reads.append(1)
        return ledger[0]

    monkeypatch.setattr(storage_access, "_read_source_records", read)

    @check_scope.checked
    def decide() -> list[str]:
        return [r["source"]["id"] for _ in range(3) for r in storage_access._source_records()]

    assert decide() == ["a", "a", "a"]
    assert len(reads) == 1
    ledger[0] = [{"source": {"id": "b"}}]
    assert decide() == ["b", "b", "b"]
    assert len(reads) == 2
    storage_access._source_records()
    storage_access._source_records()
    assert len(reads) == 4  # outside a decision, every read is made afresh


def test_a_failed_read_is_not_kept() -> None:
    """A read that raises is raised again, not remembered as a value."""
    calls: list[int] = []

    def failing() -> int:
        calls.append(1)
        raise PermissionError("ledger unreadable")

    with check_scope.check_scope():
        for _ in range(2):
            with pytest.raises(PermissionError):
                check_scope.once(("k",), failing)
    assert len(calls) == 2
