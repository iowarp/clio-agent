"""Typed session first-turn bring-up phase timing recorder (iowarp/clio-agent#1215).

"Slow to set up new agents" was an owner PERCEPTION with zero measurement
behind it. Turn-level latency was already closed by #891 (lm_streaming 89.9%
/ ttft 6.9% / everything-else 3.1%, after the pooled-SDK-client fix); session
FIRST-TURN bring-up — workspace executor build, cold fleet mount, blueprint
resolution — had no timing instrumentation anywhere. This module is that
measurement. It is deliberately just a recorder: it does not move or change
any bring-up LOGIC, and it does not itself decide anything (RULE 1 of
``.claude/CLAUDE.md`` — no deterministic decision-making baked in here).

THE #891 ATTRIBUTION CONTRACT
Every millisecond of the recorded window is accounted for one of two ways:
attributed to a named phase, or reported as ``unattributed_ms`` — never
silently dropped.

- ``unattributed_ms`` is DERIVED (``total_ms - attributed_ms``), not
  independently accumulated, so ``attributed_ms + unattributed_ms ==
  total_ms`` holds by construction (float rounding aside — see
  :meth:`BringupSummary.is_fully_attributed`).
- Phases nest via an explicit LIFO stack (:meth:`BringupTimer.start_phase` /
  :meth:`BringupTimer.end_phase`): only OUTERMOST (depth-0) spans feed
  ``attributed_ms`` — a nested child's duration is a SUBSET of its parent's
  window, so counting both would double-count wall time. By the stack
  discipline, two depth-0 spans can never overlap (starting a new phase
  while one is already open nests it, it does not sit beside it), so their
  sum is an exact, non-overlapping partition of the covered wall time. Every
  phase — nested or not — still appears individually in
  :attr:`BringupSummary.phases` for diagnosis; only the depth-0 SUM feeds the
  attribution equality.
- A phase still open when :meth:`BringupTimer.finish` runs is force-closed
  at that moment (its wall time is still counted, never a silent gap) AND
  named in :attr:`BringupSummary.unclosed_phase_names` — surfaced, never
  absorbed.

USAGE
    timer = BringupTimer(session_id=sid)
    timer.start_phase("session.create")
    ...
    timer.end_phase("session.create")
    ...
    summary = timer.finish()  # force-closes stragglers, emits bringup.summary

Each closed phase emits its own ``bringup.phase`` stream_audit row (one per
phase, carrying its elapsed ms); :meth:`finish` emits exactly one
``bringup.summary`` row with absolute ms AND percentages. Both ride
:func:`~clio_agent.runtime.stream_audit.stream_audit`, which is already a
no-op unless ``CLIO_STREAM_AUDIT_LOG`` (or ``debug.stream_audit_log``) is
configured — the same gate every other stream_audit call site in this repo
relies on, so there is no separate on/off switch to maintain here.

``lm.ttft`` is intentionally NOT instrumented by this module — it is read
from existing ``stream_audit`` rows at analysis time (the LM lane is the
providers boundary, out of scope here).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

# Float-rounding slack for the attribution-contract equality check — well
# under a microsecond, so it never masks a real accounting bug.
_EPSILON_MS = 1e-6


@dataclass(frozen=True)
class PhaseRecord:
    """One phase span as reported in a :class:`BringupSummary`.

    ``depth`` is the LIFO nesting depth at open time (0 = top-level, feeds
    ``BringupSummary.attributed_ms``; >0 = nested, reported but excluded from
    that sum to avoid double-counting). ``forced_close`` is True when the
    span was closed by :meth:`BringupTimer.finish` (still open) or as a
    casualty of a mismatched :meth:`BringupTimer.end_phase` call, rather than
    by its own matching ``end_phase``.
    """

    name: str
    depth: int
    start_offset_ms: float
    elapsed_ms: float
    forced_close: bool = False


@dataclass(frozen=True)
class BringupSummary:
    """A point-in-time (or final, via :meth:`BringupTimer.finish`) accounting.

    ``attributed_ms`` sums only depth-0 phase spans; ``unattributed_ms`` is
    ``total_ms - attributed_ms`` by construction, so
    :meth:`is_fully_attributed` holds for any summary this module produces.
    """

    session_id: str
    total_ms: float
    attributed_ms: float
    unattributed_ms: float
    phases: tuple[PhaseRecord, ...]
    unclosed_phase_names: tuple[str, ...]

    def is_fully_attributed(self, *, epsilon_ms: float = _EPSILON_MS) -> bool:
        """True when ``attributed_ms + unattributed_ms == total_ms`` within ``epsilon_ms``."""

        return abs((self.attributed_ms + self.unattributed_ms) - self.total_ms) <= epsilon_ms

    def as_dict(self) -> dict[str, Any]:
        """Wire/audit-row shape: absolute ms AND percentages (#1215)."""

        def pct(ms: float) -> float:
            return round((ms / self.total_ms) * 100.0, 3) if self.total_ms > 0 else 0.0

        return {
            "session_id": self.session_id,
            "total_ms": round(self.total_ms, 3),
            "attributed_ms": round(self.attributed_ms, 3),
            "attributed_pct": pct(self.attributed_ms),
            "unattributed_ms": round(self.unattributed_ms, 3),
            "unattributed_pct": pct(self.unattributed_ms),
            "unclosed_phase_names": list(self.unclosed_phase_names),
            "phases": [
                {
                    "name": p.name,
                    "depth": p.depth,
                    "start_offset_ms": round(p.start_offset_ms, 3),
                    "elapsed_ms": round(p.elapsed_ms, 3),
                    "pct": pct(p.elapsed_ms),
                    "forced_close": p.forced_close,
                }
                for p in self.phases
            ],
        }


@dataclass
class _OpenSpan:
    name: str
    start: float
    depth: int


class BringupTimer:
    """Records one session's first-turn bring-up phases (#1215).

    Not thread-safe by design: bring-up for one session is one sequential
    call chain (session.create -> turn.accept -> enrichment ->
    workspace.lease -> blueprint.resolve -> fleet.mount); a caller that fans
    out concurrently should use one timer per concurrent branch and merge
    externally rather than share a single instance across threads.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._t0 = time.monotonic()
        self._stack: list[_OpenSpan] = []
        self._closed: list[PhaseRecord] = []
        self._finished = False
        self._final_summary: Optional[BringupSummary] = None

    def start_phase(self, name: str) -> None:
        """Open a named phase (nests under any currently-open phase).

        A call after :meth:`finish` is a rejected late op — audited, never
        silently absorbed into whatever session/timer comes next."""

        if self._finished:
            logger.warning("bringup_timer late_start session_id=%s phase=%s", self.session_id, name)
            stream_audit(
                "bringup.late_op", session_id=self.session_id, op="start_phase", phase=name
            )
            return
        self._stack.append(_OpenSpan(name=name, start=time.monotonic(), depth=len(self._stack)))

    def end_phase(self, name: str) -> None:
        """Close the named phase, emitting its ``bringup.phase`` audit row.

        Expects LIFO discipline (``name`` is the innermost open phase).
        Instrumentation must never break the request it observes, so a
        mismatch never raises: if ``name`` is open somewhere on the stack
        but not on top, every span above it is force-closed too (each
        marked ``forced_close=True``, each still fully time-accounted); if
        ``name`` is not open at all, this is a no-op, audited as a typed
        mismatch rather than silently ignored.
        """

        if self._finished:
            logger.warning("bringup_timer late_end session_id=%s phase=%s", self.session_id, name)
            stream_audit("bringup.late_op", session_id=self.session_id, op="end_phase", phase=name)
            return
        if not any(span.name == name for span in self._stack):
            logger.warning(
                "bringup_timer end_phase_not_open session_id=%s phase=%s", self.session_id, name
            )
            stream_audit(
                "bringup.phase_mismatch", session_id=self.session_id, phase=name, reason="not_open"
            )
            return
        now = time.monotonic()
        while self._stack:
            span = self._stack.pop()
            matched = span.name == name
            self._close_span(span, now, forced_close=not matched)
            if matched:
                break

    def _close_span(self, span: _OpenSpan, now: float, *, forced_close: bool) -> None:
        elapsed_ms = max(0.0, (now - span.start) * 1000.0)
        record = PhaseRecord(
            name=span.name,
            depth=span.depth,
            start_offset_ms=(span.start - self._t0) * 1000.0,
            elapsed_ms=elapsed_ms,
            forced_close=forced_close,
        )
        self._closed.append(record)
        stream_audit(
            "bringup.phase",
            session_id=self.session_id,
            phase=span.name,
            depth=span.depth,
            elapsed_ms=round(elapsed_ms, 3),
            forced_close=forced_close,
        )

    def summary(self, *, _now: Optional[float] = None) -> BringupSummary:
        """A PURE point-in-time snapshot — never mutates recorder state.

        A still-open phase contributes its elapsed-so-far to the snapshot
        (using ``_now`` or the current clock) WITHOUT being removed from the
        live stack, so calling this mid-flight is safe and repeatable. Use
        :meth:`finish` to actually settle the recorder.
        """

        now = _now if _now is not None else time.monotonic()
        phases = list(self._closed)
        unclosed_names: list[str] = []
        for span in self._stack:
            phases.append(
                PhaseRecord(
                    name=span.name,
                    depth=span.depth,
                    start_offset_ms=(span.start - self._t0) * 1000.0,
                    elapsed_ms=max(0.0, (now - span.start) * 1000.0),
                    forced_close=True,
                )
            )
            unclosed_names.append(span.name)
        return self._build_summary(now, phases, unclosed_names)

    def _build_summary(
        self, now: float, phases: list[PhaseRecord], unclosed_names: list[str]
    ) -> BringupSummary:
        total_ms = max(0.0, (now - self._t0) * 1000.0)
        attributed_ms = sum(p.elapsed_ms for p in phases if p.depth == 0)
        # Defensive clamp: the LIFO stack discipline makes attributed_ms > total_ms
        # unreachable for correctly paired start/end calls, but a clamp (rather
        # than a negative unattributed_ms) keeps the contract holding even if a
        # future edit breaks that invariant, instead of reporting a nonsensical
        # negative "unattributed" number.
        attributed_ms = min(attributed_ms, total_ms)
        unattributed_ms = total_ms - attributed_ms
        return BringupSummary(
            session_id=self.session_id,
            total_ms=total_ms,
            attributed_ms=attributed_ms,
            unattributed_ms=unattributed_ms,
            phases=tuple(phases),
            unclosed_phase_names=tuple(unclosed_names),
        )

    def finish(self) -> BringupSummary:
        """Settle the recorder: force-close any stragglers for real, emit ONE
        ``bringup.summary`` stream_audit row, and freeze further phases.

        Idempotent: a second call returns the SAME settled summary without
        re-emitting (audited as a late op), mirroring the transcript
        ledger's late-op discipline (``transcript.late_op``).
        """

        if self._finished:
            stream_audit("bringup.late_op", session_id=self.session_id, op="finish")
            assert self._final_summary is not None  # set the first time finish() ran
            return self._final_summary

        now = time.monotonic()
        unclosed_names = [span.name for span in self._stack]
        while self._stack:
            span = self._stack.pop()
            self._close_span(span, now, forced_close=True)
        summary = self._build_summary(now, list(self._closed), unclosed_names)
        self._finished = True
        self._final_summary = summary
        stream_audit("bringup.summary", **summary.as_dict())
        return summary


__all__ = ["BringupTimer", "BringupSummary", "PhaseRecord"]
