"""Pure invariant tests for the bring-up phase timing recorder (iowarp/clio-agent#1215).

No live cold/warm capture here (that runs later against a running stack, per
the issue) — this is the "measurement IS the deliverable" pure-recorder half:
the #891 attribution contract (``attributed_ms + unattributed_ms ==
total_ms``), nested phases never double-counting, and an unclosed phase
surfacing by name rather than being silently absorbed. A deterministic fake
clock replaces ``time.perf_counter`` (review D2: the module's clock, house
style -- NOT ``time.monotonic``) so elapsed values are exact, not
timing-flake-prone.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.bringup_timing import BringupTimer


class _FakeClock:
    """A monotonically-advancing, fully deterministic stand-in for time.perf_counter."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _install_clock(monkeypatch: pytest.MonkeyPatch, start: float = 0.0) -> _FakeClock:
    clock = _FakeClock(start)
    monkeypatch.setattr(bringup_timing.time, "perf_counter", clock)
    return clock


def _capture_audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.runtime.bringup_timing.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    return audits


# ---------------------------------------------------------------------------
# 1. Core attribution contract — sequential phases with gaps.
# ---------------------------------------------------------------------------


def test_sequential_phases_attribution_contract_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch, start=100.0)
    timer = BringupTimer(session_id="s1")

    clock.advance(0.010)  # 10ms unattributed gap before the first phase
    timer.start_phase("a")
    clock.advance(0.050)  # 50ms attributed to "a"
    timer.end_phase("a")
    clock.advance(0.020)  # 20ms unattributed gap between phases
    timer.start_phase("b")
    clock.advance(0.030)  # 30ms attributed to "b"
    timer.end_phase("b")

    summary = timer.finish()
    # Sabotage: sum attributed_ms independently instead of deriving
    # unattributed_ms = total - attributed -> a rounding/logic bug here would
    # break this equality; deriving it makes the contract hold by construction.
    assert summary.is_fully_attributed()
    assert summary.total_ms == pytest.approx(110.0, abs=1e-6)
    assert summary.attributed_ms == pytest.approx(80.0, abs=1e-6)
    assert summary.unattributed_ms == pytest.approx(30.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 2. Nested phases never double-count.
# ---------------------------------------------------------------------------


def test_nested_phases_do_not_double_count(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s2")

    timer.start_phase("parent")  # depth 0
    clock.advance(0.010)
    timer.start_phase("child")  # depth 1, nested inside parent
    clock.advance(0.020)
    timer.end_phase("child")  # child elapsed = 20ms
    clock.advance(0.010)
    timer.end_phase("parent")  # parent span = 10 + 20 + 10 = 40ms total

    summary = timer.finish()
    # Sabotage: sum ALL phases regardless of depth (parent 40 + child 20 = 60)
    # -> attributed_ms would exceed total_ms (40) and is_fully_attributed()
    # would still "hold" only because of the defensive clamp, but the real
    # regression is caught by the exact-value assertion below.
    assert summary.is_fully_attributed()
    assert summary.attributed_ms == pytest.approx(40.0, abs=1e-6)
    assert summary.unattributed_ms == pytest.approx(0.0, abs=1e-6)
    assert summary.total_ms == pytest.approx(40.0, abs=1e-6)

    by_name = {p.name: p for p in summary.phases}
    assert set(by_name) == {"parent", "child"}
    assert by_name["parent"].depth == 0
    assert by_name["child"].depth == 1
    assert by_name["child"].elapsed_ms == pytest.approx(20.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. An unclosed phase is surfaced by name, never silently absorbed.
# ---------------------------------------------------------------------------


def test_unclosed_phase_is_surfaced_by_name_and_still_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s3")

    timer.start_phase("a")
    clock.advance(0.010)
    timer.end_phase("a")
    clock.advance(0.005)
    timer.start_phase("b")  # never end_phase'd
    clock.advance(0.020)

    summary = timer.finish()
    # Sabotage: drop stragglers from the stack without recording them (a bare
    # ``self._stack.clear()``) -> "b" disappears from unclosed_phase_names AND
    # its 20ms vanishes from total attribution -> both assertions below red.
    assert "b" in summary.unclosed_phase_names
    assert summary.is_fully_attributed()
    b_record = next(p for p in summary.phases if p.name == "b")
    assert b_record.forced_close is True
    assert b_record.elapsed_ms == pytest.approx(20.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Supporting coverage: audit emission, idempotency, mismatch handling, purity.
# ---------------------------------------------------------------------------


def test_end_phase_emits_one_bringup_phase_row_per_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits = _capture_audits(monkeypatch)
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s4")

    timer.start_phase("a")
    clock.advance(0.010)
    timer.end_phase("a")
    timer.start_phase("b")
    clock.advance(0.020)
    timer.end_phase("b")

    rows = [fields for stage, fields in audits if stage == "bringup.phase"]
    # Sabotage: emit per delta/event instead of once per closed phase -> this
    # length assertion goes red.
    assert [r["phase"] for r in rows] == ["a", "b"]
    assert rows[0]["elapsed_ms"] == pytest.approx(10.0, abs=1e-6)
    assert rows[1]["elapsed_ms"] == pytest.approx(20.0, abs=1e-6)


def test_finish_emits_exactly_one_summary_row_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits = _capture_audits(monkeypatch)
    timer = BringupTimer(session_id="s5")
    timer.start_phase("a")
    timer.end_phase("a")

    first = timer.finish()
    second = timer.finish()

    assert first == second
    summary_rows = [fields for stage, fields in audits if stage == "bringup.summary"]
    # Sabotage: re-emit bringup.summary on every finish() call -> len != 1.
    assert len(summary_rows) == 1
    late_rows = [
        fields
        for stage, fields in audits
        if stage == "bringup.late_op" and fields.get("op") == "finish"
    ]
    assert len(late_rows) == 1


def test_as_dict_percentages_sum_to_100(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s6")
    timer.start_phase("a")
    clock.advance(0.030)
    timer.end_phase("a")
    clock.advance(0.020)  # unattributed gap

    d = timer.finish().as_dict()
    assert d["attributed_pct"] + d["unattributed_pct"] == pytest.approx(100.0, abs=0.01)
    assert d["total_ms"] == pytest.approx(50.0, abs=1e-3)


def test_end_phase_not_open_is_audited_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    audits = _capture_audits(monkeypatch)
    timer = BringupTimer(session_id="s7")

    timer.end_phase("never_opened")  # must not raise

    rows = [fields for stage, fields in audits if stage == "bringup.phase_mismatch"]
    assert len(rows) == 1
    assert rows[0]["phase"] == "never_opened"
    assert rows[0]["reason"] == "not_open"


def test_end_phase_mismatched_order_force_closes_the_casualty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = BringupTimer(session_id="s8")
    timer.start_phase("outer")
    timer.start_phase("inner")
    # Caller forgets to close "inner" and closes "outer" directly instead.
    timer.end_phase("outer")

    summary = timer.finish()
    by_name = {p.name: p for p in summary.phases}
    assert by_name["inner"].forced_close is True
    assert by_name["outer"].forced_close is False
    assert summary.is_fully_attributed()


def test_start_phase_after_finish_is_audited_not_silently_absorbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits = _capture_audits(monkeypatch)
    timer = BringupTimer(session_id="s9")
    timer.start_phase("a")
    timer.end_phase("a")
    first = timer.finish()

    timer.start_phase("b")  # after finish -- rejected, must not mutate the settled summary
    second = timer.finish()

    assert first == second
    late_rows = [
        fields
        for stage, fields in audits
        if stage == "bringup.late_op" and fields.get("op") == "start_phase"
    ]
    assert len(late_rows) == 1
    assert late_rows[0]["phase"] == "b"


def test_summary_is_pure_and_repeatable(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s10")
    timer.start_phase("a")
    clock.advance(0.010)

    snap1 = timer.summary()
    clock.advance(0.010)
    snap2 = timer.summary()

    # Sabotage: have summary() mutate self._stack (e.g. pop the open span) ->
    # the phase would vanish from unclosed_phase_names on the second call.
    assert snap1.unclosed_phase_names == ("a",)
    assert snap2.unclosed_phase_names == ("a",)
    assert snap2.total_ms > snap1.total_ms

    # summary() never actually closed the phase -- end_phase still works.
    timer.end_phase("a")
    final = timer.finish()
    assert final.unclosed_phase_names == ()


# ---------------------------------------------------------------------------
# Opus adversarial review fix-first findings (D2 clock, D3 clamp, R1, R3).
# ---------------------------------------------------------------------------


def test_summary_after_finish_returns_the_frozen_final_not_a_growing_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review R1: calling summary() after finish() used to recompute against
    the CURRENT clock (total_ms kept growing with wall time even though the
    timer was "done" -- a settled 0ms timer read back as 63ms on a later
    call). It must now return the SAME frozen summary finish() produced."""

    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s11")
    timer.start_phase("a")
    clock.advance(0.005)
    timer.end_phase("a")

    settled = timer.finish()
    clock.advance(9999.0)  # a large amount of "later" wall-clock time passes

    # Sabotage: make summary() always recompute via time.perf_counter() /
    # self._t0 regardless of self._finished -> this returns a summary with
    # total_ms inflated by the 9999s advance above -> red.
    again = timer.summary()
    assert again == settled
    assert again.total_ms == pytest.approx(5.0, abs=1e-6)


def test_attribution_violation_is_audited_and_surfaced_not_silently_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review D3: the min(attributed_ms, total_ms) clamp that keeps the
    contract holding must never run silently -- a nonzero overattributed_ms,
    a logger.warning, and a bringup.attribution_violation stream_audit row
    are all required when the raw depth-0 sum exceeds total_ms (a
    phase-open/close bug, which the LIFO stack normally makes unreachable --
    forced here via a synthetic phase record to prove the guard exists)."""

    audits = _capture_audits(monkeypatch)
    timer = BringupTimer(session_id="s12")
    # Force the otherwise-unreachable over-attribution path: hand _build_summary
    # a closed depth-0 phase whose own elapsed_ms exceeds the wall-clock total.
    bogus_phase = bringup_timing.PhaseRecord(
        name="bogus", depth=0, start_offset_ms=0.0, elapsed_ms=999_000.0
    )
    summary = timer._build_summary(timer._t0 + 0.001, [bogus_phase], [])

    # Sabotage: keep the silent min() clamp with no signal -> is_fully_attributed()
    # still holds (by construction) but overattributed_ms stays 0.0 and no audit
    # row exists -> both assertions below catch that regression.
    assert summary.is_fully_attributed()
    assert summary.overattributed_ms == pytest.approx(999_000.0 - summary.total_ms, abs=1e-3)
    assert summary.overattributed_ms > 0.0
    assert summary.as_dict()["overattributed_ms"] == pytest.approx(
        summary.overattributed_ms, abs=1e-3
    )
    violations = [f for stage, f in audits if stage == "bringup.attribution_violation"]
    assert len(violations) == 1
    assert violations[0]["session_id"] == "s12"
    assert violations[0]["overattributed_ms"] > 0


def test_attribution_violation_absent_on_the_healthy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin: ordinary correctly-paired phases never trip D3's guard."""

    audits = _capture_audits(monkeypatch)
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s13")
    timer.start_phase("a")
    clock.advance(0.010)
    timer.end_phase("a")

    summary = timer.finish()
    assert summary.overattributed_ms == 0.0
    assert [f for stage, f in audits if stage == "bringup.attribution_violation"] == []


def test_phase_context_manager_closes_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s14")

    with timer.phase("workspace.lease"):
        clock.advance(0.015)

    summary = timer.finish()
    assert summary.is_fully_attributed()
    assert summary.unclosed_phase_names == ()
    lease = next(p for p in summary.phases if p.name == "workspace.lease")
    assert lease.elapsed_ms == pytest.approx(15.0, abs=1e-6)
    assert lease.forced_close is False


def test_phase_context_manager_closes_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review R3: the whole point is exception safety -- a raise inside the
    `with` block must still close (and correctly time) the phase, and the
    original exception must still propagate unchanged."""

    clock = _install_clock(monkeypatch)
    timer = BringupTimer(session_id="s15")

    with pytest.raises(ValueError, match="boom"):
        with timer.phase("blueprint.resolve"):
            clock.advance(0.008)
            raise ValueError("boom")

    summary = timer.finish()
    # Sabotage: drop the try/finally (bare start_phase/end_phase call, or no
    # end_phase at all on the raise path) -> the phase is missing or reported
    # under unclosed_phase_names as forced -> either way these assertions red.
    assert summary.unclosed_phase_names == ()
    resolved = next(p for p in summary.phases if p.name == "blueprint.resolve")
    assert resolved.elapsed_ms == pytest.approx(8.0, abs=1e-6)
    assert resolved.forced_close is False
    assert summary.is_fully_attributed()
