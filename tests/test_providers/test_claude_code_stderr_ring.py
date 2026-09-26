"""The bounded stderr ring buffer for the claude_code SDK transport (B17).

Pins: bounded (never grows past capacity), tail ordering, and clear-on-reconnect
semantics (a fresh CLI subprocess must never inherit a prior process's lines).
"""

from __future__ import annotations

from clio_agent.providers.claude_code_stderr_ring import RING_CAPACITY, StderrRing


def test_ring_starts_empty() -> None:
    ring = StderrRing()
    assert len(ring) == 0
    assert ring.tail() == ""


def test_ring_appends_and_tails_in_order() -> None:
    ring = StderrRing()
    ring.append("line one\n")
    ring.append("line two")
    assert ring.tail() == "line one\nline two"
    assert len(ring) == 2


def test_ring_drops_blank_lines() -> None:
    ring = StderrRing()
    ring.append("\n")
    ring.append("")
    assert len(ring) == 0


def test_ring_is_bounded_at_capacity(monkeypatch) -> None:
    """SABOTAGE: use an unbounded list instead of a maxlen deque -> a
    pathologically noisy CLI grows this without bound -> red."""
    ring = StderrRing()
    for i in range(RING_CAPACITY + 50):
        ring.append(f"line {i}")
    assert len(ring) == RING_CAPACITY
    # Only the LAST RING_CAPACITY lines survive -- the tail, not the head.
    assert ring.tail(1) == f"line {RING_CAPACITY + 49}"


def test_ring_tail_n_returns_only_the_last_n_lines() -> None:
    ring = StderrRing()
    for i in range(5):
        ring.append(f"line {i}")
    assert ring.tail(2) == "line 3\nline 4"


def test_ring_clear_drops_everything() -> None:
    ring = StderrRing()
    ring.append("stale from a prior process")
    ring.clear()
    assert len(ring) == 0
    assert ring.tail() == ""
