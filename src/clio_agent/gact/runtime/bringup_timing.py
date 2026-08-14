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

CLOCK (review D2): every timestamp uses ``time.perf_counter()`` (house style,
``gact/agents/builders.py``'s ``call_tool`` timing), not ``time.monotonic()``
— on Windows ``time.monotonic()``'s effective resolution can be ~15.6ms (the
system timer tick), which read a real 40ms phase as 31ms and a real 10ms
phase as 0.0ms in review probes. ``perf_counter()`` is still monotonic
(safe for elapsed-time subtraction) but backed by the highest-resolution
counter the platform exposes.

SEAM WIRING (S5) — ``BringupTimerRegistry`` + :func:`timer_for_session` /
:func:`finish_bringup` are the per-session STORAGE half, so seam call sites
across ``routes/sessions.py`` / ``turn.py`` / ``enrichment.py`` /
``turn_forward.py`` stay ONE line each (RULE 4: no new store — this is a
small, LRU-capped, thread-safe registry on ``app.state.bringup_timers``,
ephemeral first-turn state, never a persisted projection). A plain
``threading.Lock`` guards it because bring-up spans an HTTP handler
(session.create), the turn's own background task (turn.accept_gap /
enrichment / workspace.lease / blueprint.resolve), and — were fleet.mount
wired (see below) — an executor thread; a lock is simpler and more
obviously correct than merging per-thread partial timers.

PHASE LAYOUT is flat where the real code is genuinely sequential
(``session.create`` -> ``turn.accept_gap`` -> ``enrichment``), but
``blueprint.resolve`` NESTS inside ``workspace.lease`` (depth 1) rather than
sitting flat beside it: ``forward_turn`` holds the workspace lease
(``_tool_session_context``) for its ENTIRE body, which structurally CONTAINS
blueprint resolution — an early review pass tried instrumenting these as two
independent depth-0 phases and produced a self-contradictory summary
(is_fully_attributed() held only via the D3 clamp while the raw phase list
summed to 221% of total_ms — the exact double-count the #891 contract exists
to prevent). Nesting is the CORRECT fix, not a workaround: only the depth-0
sum (``workspace.lease``) feeds ``attributed_ms``, and ``blueprint.resolve``
still appears in ``BringupSummary.phases`` for diagnosis.

``fleet.mount`` (the ``SyncMCPToolExecutor`` first-build cost,
``tools/execution.py``'s ``create_sync_tool_executor`` reached via
``ClioAgent._active_tool_executor`` in ``agent.py``) is deliberately NOT
wired in this slice — a genuine architecture blocker, not an oversight:
that executor cache is keyed by WORKSPACE ROOT, not session id (a shared
workspace's fleet is built once, by whichever session's first tool call
gets there first), and ``agent.py`` / ``tools/execution.py`` must stay
gact-agnostic (``tools.execution`` imports no ``gact``, per
``gact/runtime/app_state.py``'s layering note) — reading the active
session id there would violate that boundary, and threading a session_id
parameter through the constructor is a hot-path signature change shared by
the CLI and GACT. Wiring it needs its own design pass; #1215 stays open.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

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

    ``overattributed_ms`` (review D3) is normally ``0.0``: it is the amount
    by which the RAW depth-0 sum exceeded ``total_ms`` BEFORE the defensive
    clamp in :meth:`BringupTimer._build_summary` silently capped
    ``attributed_ms`` at ``total_ms``. That clamp alone was itself a silent
    fallback — it kept :meth:`is_fully_attributed` returning True even while
    swallowing seconds of over-attribution in a review probe. The clamp
    still runs (the contract must always hold), but a nonzero
    ``overattributed_ms`` here — surfaced in :meth:`as_dict` and via the
    ``bringup.attribution_violation`` stream_audit row emitted alongside it
    — is the loud signal that a phase-open/close bug elsewhere over-counted.
    """

    session_id: str
    total_ms: float
    attributed_ms: float
    unattributed_ms: float
    phases: tuple[PhaseRecord, ...]
    unclosed_phase_names: tuple[str, ...]
    overattributed_ms: float = 0.0

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
            "overattributed_ms": round(self.overattributed_ms, 3),
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
        self._t0 = time.perf_counter()
        self._stack: list[_OpenSpan] = []
        self._closed: list[PhaseRecord] = []
        self._finished = False
        self._final_summary: Optional[BringupSummary] = None

    @property
    def is_finished(self) -> bool:
        """True once :meth:`finish` has settled this timer."""

        return self._finished

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
        self._stack.append(_OpenSpan(name=name, start=time.perf_counter(), depth=len(self._stack)))

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
        now = time.perf_counter()
        while self._stack:
            span = self._stack.pop()
            matched = span.name == name
            self._close_span(span, now, forced_close=not matched)
            if matched:
                break

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Exception-safe phase span (review R3): ``start_phase`` on enter,
        ``end_phase`` on exit via try/finally — the recommended shape for
        seam call sites (``with timer.phase("workspace.lease"): ...``) so a
        raised exception mid-phase still closes and times it (the elapsed
        wall time up to the raise is real and still gets attributed) instead
        of leaking an unclosed phase that only :meth:`finish` would ever
        notice. The exception itself always propagates unchanged.
        """

        self.start_phase(name)
        try:
            yield
        finally:
            self.end_phase(name)

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
        """A point-in-time snapshot that never mutates the live stack/closed
        list — a still-open phase contributes its elapsed-so-far WITHOUT
        being removed from the stack, so calling this mid-flight is safe and
        repeatable. Once :meth:`finish` has settled the recorder, this
        returns that SAME frozen summary (ignoring ``_now``) instead of
        recomputing against the current clock — review R1: recomputing kept
        ``total_ms`` growing with wall-clock time even after the timer was
        "done" (a settled 0ms timer read back as 63ms on a later call).
        """

        if self._finished:
            assert self._final_summary is not None  # set the first time finish() ran
            return self._final_summary
        now = _now if _now is not None else time.perf_counter()
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
        raw_attributed_ms = sum(p.elapsed_ms for p in phases if p.depth == 0)
        overattributed_ms = 0.0
        if raw_attributed_ms > total_ms:
            # Review D3: the clamp below is itself a silent fallback if it runs
            # quietly -- a prior version capped attributed_ms at total_ms with no
            # signal, so is_fully_attributed() kept returning True while seconds
            # of over-attribution (a phase-open/close bug) vanished unnoticed.
            # The clamp still has to run (the contract must always hold for
            # every summary this module hands out), but now it is LOUD.
            overattributed_ms = raw_attributed_ms - total_ms
            logger.warning(
                "bringup_timer attribution_violation session_id=%s overattributed_ms=%.3f "
                "attributed_ms=%.3f total_ms=%.3f",
                self.session_id,
                overattributed_ms,
                raw_attributed_ms,
                total_ms,
            )
            stream_audit(
                "bringup.attribution_violation",
                session_id=self.session_id,
                overattributed_ms=round(overattributed_ms, 3),
                attributed_ms=round(raw_attributed_ms, 3),
                total_ms=round(total_ms, 3),
            )
        attributed_ms = min(raw_attributed_ms, total_ms)
        unattributed_ms = total_ms - attributed_ms
        return BringupSummary(
            session_id=self.session_id,
            total_ms=total_ms,
            attributed_ms=attributed_ms,
            unattributed_ms=unattributed_ms,
            phases=tuple(phases),
            unclosed_phase_names=tuple(unclosed_names),
            overattributed_ms=overattributed_ms,
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

        now = time.perf_counter()
        unclosed_names = [span.name for span in self._stack]
        while self._stack:
            span = self._stack.pop()
            self._close_span(span, now, forced_close=True)
        summary = self._build_summary(now, list(self._closed), unclosed_names)
        self._finished = True
        self._final_summary = summary
        stream_audit("bringup.summary", **summary.as_dict())
        return summary


class _NullBringupTimer:
    """No-op stand-in for a session whose bring-up has already settled.

    Once :meth:`BringupTimerRegistry.finish` runs for a session (its first
    turn is done), every LATER call to :func:`timer_for_session` for that
    same session_id returns this singleton instead of ``None`` -- so every
    seam call site can call ``start_phase``/``end_phase``/``phase`` UNCONDITIONALLY,
    with no ``if timer is not None`` guard, and stay a genuine one-liner. This
    is expected on every turn after the first, not an error.
    """

    def start_phase(self, name: str) -> None:
        return None

    def end_phase(self, name: str) -> None:
        return None

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        yield


_NULL_TIMER = _NullBringupTimer()


class BringupTimerRegistry:
    """Bounded, thread-safe per-session :class:`BringupTimer` store (#1215 S5).

    Lives on ``app.state.bringup_timers`` — small, ephemeral, first-turn-only
    state (RULE 4: not a new persisted store; gone once a session's bring-up
    settles or the LRU cap evicts it). A plain :class:`threading.Lock` guards
    it: session.create runs on the event loop, turn.accept_gap/enrichment/
    workspace.lease/blueprint.resolve run on the turn's background task, and
    a future fleet.mount wiring would fire from an executor thread — a lock
    is simpler and more obviously correct than merging per-thread partials.
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        self._lock = threading.Lock()
        self._timers: "OrderedDict[str, BringupTimer]" = OrderedDict()
        # Sessions whose bring-up has already settled (finish() ran) — a
        # LATER get() for these returns the null timer, never a fresh one
        # (bring-up is a FIRST-TURN-ONLY concept; turn 2+ must not silently
        # start measuring a whole new "bring-up" for an already-warm session).
        self._settled: set[str] = set()
        self._max_entries = max_entries

    def get_or_create(self, session_id: str) -> "BringupTimer | _NullBringupTimer":
        with self._lock:
            if session_id in self._settled:
                return _NULL_TIMER
            timer = self._timers.get(session_id)
            if timer is None:
                timer = BringupTimer(session_id=session_id)
                self._timers[session_id] = timer
                self._evict_over_cap_locked()
            else:
                self._timers.move_to_end(session_id)
            return timer

    def finish(self, session_id: str) -> Optional[BringupSummary]:
        """Settle + evict ``session_id``'s timer, returning its summary.

        ``None`` when bring-up was never started for this session (a normal
        no-op: the session predates this instrumentation, or its bring-up
        already settled and was evicted by an earlier ``finish()`` call --
        idempotent, safe to call from more than one seam). A never-started
        session is NOT marked settled here -- only a genuinely-timed session
        graduates to the null-timer path, so a stray/defensive finish() call
        can never pre-emptively block that session's real first-turn timer.
        """

        with self._lock:
            timer = self._timers.pop(session_id, None)
            if timer is not None:
                self._settled.add(session_id)
        if timer is None:
            return None
        return timer.finish()

    def _evict_over_cap_locked(self) -> None:
        while len(self._timers) > self._max_entries:
            evicted_sid, evicted_timer = self._timers.popitem(last=False)
            self._settled.add(evicted_sid)
            if not evicted_timer.is_finished:
                # An LRU-capped registry evicting a still-open timer is a real
                # anomaly (bring-up should settle in seconds) -- settle it so
                # its partial data is not silently lost rather than dropping
                # it unfinished.
                logger.warning(
                    "bringup_timer_registry lru_evicted_unfinished session_id=%s", evicted_sid
                )
                evicted_timer.finish()


def timer_for_session(app: "FastAPI", session_id: str) -> "BringupTimer | _NullBringupTimer":
    """Thin, ONE-LINE seam entry point: get-or-create ``session_id``'s live
    bring-up timer (or the no-op null timer for a session past its first
    turn). The only import a seam call site needs."""

    return _registry(app).get_or_create(session_id)


def finish_bringup(app: "FastAPI", session_id: str) -> Optional[BringupSummary]:
    """Settle + evict ``session_id``'s bring-up timer (idempotent no-op if
    never started or already settled)."""

    return _registry(app).finish(session_id)


def _registry(app: "FastAPI") -> BringupTimerRegistry:
    existing = getattr(app.state, "bringup_timers", None)
    if isinstance(existing, BringupTimerRegistry):
        return existing
    registry = BringupTimerRegistry()
    app.state.bringup_timers = registry
    return registry


__all__ = [
    "BringupTimer",
    "BringupSummary",
    "BringupTimerRegistry",
    "PhaseRecord",
    "timer_for_session",
    "finish_bringup",
]
