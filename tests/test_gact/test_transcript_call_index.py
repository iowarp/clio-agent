"""A tool result finds its call through the index, not a scan of the whole turn.

On P7 each result scanned about 4,000 parts (2026-11-02).
"""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.transcript_call_index import ToolCallIndex


def _part(kind: str, call_id: str = "") -> SimpleNamespace:
    return SimpleNamespace(type=kind, call_id=call_id)


class _Counted(list):
    """A ledger that counts how many parts are read."""

    reads = 0

    def __getitem__(self, item):  # type: ignore[no-untyped-def]
        value = super().__getitem__(item)
        if isinstance(item, slice):
            type(self).reads += len(value)
        return value


def test_each_lookup_reads_only_new_parts() -> None:
    """SABOTAGE: reset ``_scanned`` to 0 on every lookup and the reads grow quadratically."""
    ledger = _Counted()
    index = ToolCallIndex()
    _Counted.reads = 0
    for n in range(500):
        ledger.append(_part("tool_call", f"c{n}"))
        ledger.append(_part("tool_result", f"c{n}"))
        assert index.parts(ledger, f"c{n}") == [ledger[-2]]
    assert _Counted.reads <= 2 * 500


def test_a_removal_before_the_scanned_point_rebuilds() -> None:
    first, text, second = _part("tool_call", "a"), _part("text"), _part("tool_call", "b")
    ledger = [first, text, second]
    index = ToolCallIndex()
    assert index.parts(ledger, "b") == [second]
    ledger.remove(text)  # the ledger drops a whitespace-only text part
    third = _part("tool_call", "c")
    ledger.append(third)
    assert index.parts(ledger, "c") == [third]
    assert index.parts(ledger, "a") == [first]
