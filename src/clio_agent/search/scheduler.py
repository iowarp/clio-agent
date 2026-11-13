"""One request queue per provider for the whole CLIO server.

Every session and every agent in it sends provider requests through
:data:`SCHEDULER`; to a provider, CLIO is one well-behaved client. Per provider
the queue:

* runs at most the tier's **concurrency** and starts requests no faster than its
  **rate** (:mod:`clio_agent.search.terms`), and fewer while the provider pushes
  back (one fewer at a time after each too-many-requests, restored after a run of
  answers);
* gives **turns fairly**: callers (sessions) take turns, first come first served
  within a caller, so eight researchers in one session do not starve another;
* **pauses the provider for everyone** on too-many-requests (its ``Retry-After``,
  or a growing backoff) and puts the request back at the head of its caller's line;
  a request whose wait would outlast its patience leaves the queue with the typed
  failure, so web search moves to the next source and literature search notes it;
* asks a provider that **failed outright** (network, 404, 5xx) once more after a
  short pause before it rests the provider;
* **counts the free allowance** this CLIO used (requests per hour or day, or
  OpenAlex dollars) and stops before the allowance runs out, typed as
  ``allowance_used``, instead of spending the network's shared allowance to zero;
* **learns** from rate-limit headers: Crossref's declared rate and concurrency,
  OpenAlex's remaining dollars, and generic ``X-RateLimit-Remaining``/``Reset``.

The allowance count is this server's since it started; other programs on the
same network share keyless allowances, so the provider's own answers stay the
final word. Cooldowns, status and questions live in
:data:`clio_agent.tools.servers.web_limits.SEARCH_LIMITS`; this module decides
*when* a request may run.

All state is guarded by one ``threading.Lock`` (tool calls run on several
threads and event loops); waiting happens outside the lock.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from clio_agent.search import http
from clio_agent.search.http import SourceError
from clio_agent.search.reasons import SearchNote, note
from clio_agent.search.terms import Terms
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS, SearchLimits

T = TypeVar("T")

#: How long a web search waits in line before it moves to the next source.
WEB_PATIENCE_S = 10.0
#: How long a literature request waits in line before the source is reported.
LITERATURE_PATIENCE_S = 30.0
#: Pause before asking a provider that failed outright once more.
RETRY_PAUSE_S = 1.0
#: Backoff for a too-many-requests answer without ``Retry-After``: first, longest.
BACKOFF_S = (2.0, 60.0)
#: Rest after a provider's first outright failure, and after a repeated one.
FAILED_REST_S = (30.0, 300.0)
#: Answers in a row after which one more request may run at once again.
RECOVER_AFTER = 20
#: Most times one request is put back in line after too-many-requests.
MAX_REQUEUES = 3
_POLL_S = 0.05


@dataclass
class _Usage:
    """Allowance spent in the current hour and day."""

    hour: str = ""
    day: str = ""
    hour_requests: int = 0
    day_requests: int = 0
    day_usd: float = 0.0

    def roll(self, now: float) -> None:
        hour = time.strftime("%Y-%m-%dT%H", time.gmtime(now))
        day = hour[:10]
        if self.day != day:
            self.day, self.day_requests, self.day_usd = day, 0, 0.0
        if self.hour != hour:
            self.hour, self.hour_requests = hour, 0


@dataclass
class _Lane:
    """One provider's queue."""

    tier: str = ""
    terms: Terms | None = None
    active: int = 0
    next_start: float = 0.0
    #: Concurrency lost to push-back; restored one at a time.
    shrink: int = 0
    answered_in_row: int = 0
    pushbacks_in_row: int = 0
    #: Caller → its waiting tickets, oldest first; the dict order is the turn order.
    lines: OrderedDict[str, deque[int]] = field(default_factory=OrderedDict)
    usage: _Usage = field(default_factory=_Usage)
    #: What the provider's headers taught: interval, concurrency, remaining dollars.
    learned_interval: float | None = None
    learned_concurrency: int | None = None
    remaining_usd: float | None = None

    def base_concurrency(self) -> int:
        """The tier's concurrency, or less when the provider declared less."""

        declared = self.terms.concurrency if self.terms else 2
        return min(declared, self.learned_concurrency or declared)

    def concurrency(self) -> int:
        """Requests allowed at once now (fewer while the provider pushes back)."""

        return max(1, self.base_concurrency() - self.shrink)

    def interval(self) -> float:
        declared = self.terms.interval_s if self.terms else 0.0
        return max(declared, self.learned_interval or 0.0)

    def next_ticket(self) -> int | None:
        for line in self.lines.values():
            if line:
                return line[0]
        return None


BUSY_DETAIL = "busy: other requests to it are ahead in CLIO's queue"


class QueueBusy(SourceError):
    """The request waited its patience in line; the provider itself is fine."""


@dataclass
class CallEvents:
    """What happened to one request in the queue, for the caller's notes."""

    waited_s: float = 0.0
    retried: list[str] = field(default_factory=list)
    requeued: int = 0


class ProviderScheduler:
    """The per-provider request queues (see the module docstring)."""

    def __init__(self, limits: SearchLimits = SEARCH_LIMITS) -> None:
        self._limits = limits
        self._lock = threading.Lock()
        self._lanes: dict[str, _Lane] = {}
        self._tickets = itertools.count(1)

    # ---------------------------------------------------------------- public
    async def call(
        self,
        source: str,
        run: Callable[[], Awaitable[T]],
        *,
        terms: Terms,
        caller: str = "",
        cost_usd: float = 0.0,
        patience_s: float = WEB_PATIENCE_S,
        events: CallEvents | None = None,
    ) -> T:
        """Run ``run`` when ``source``'s queue gives this caller a turn.

        Raises:
            SourceError: the provider failed, refused, or would not be free
                within ``patience_s``; its kind says which.
        """

        report = events if events is not None else CallEvents()
        deadline = time.monotonic() + patience_s
        began = time.monotonic()
        failed_once = False
        ticket = self._enqueue(source, terms, caller)
        try:
            while True:
                await self._turn(source, ticket, caller, deadline, cost_usd)
                report.waited_s = time.monotonic() - began
                try:
                    value = await run()
                except SourceError as exc:
                    action = self._settle_failure(source, exc, failed_once, report, deadline)
                    if action == "raise":
                        raise
                    failed_once = failed_once or exc.kind == http.FAILED
                    if action == "retry":
                        await asyncio.sleep(RETRY_PAUSE_S)
                    self._requeue(source, ticket, caller)
                    continue
                finally:
                    self._finish(source)
                self._settle_answer(source)
                return value
        finally:
            self._drop(source, ticket, caller)

    def learn(self, source: str, headers: Mapping[str, str]) -> None:
        """Take a provider's rate-limit headers into account (called by :mod:`.http`)."""

        lowered = {k.lower(): v for k, v in headers.items()}
        with self._lock:
            lane = self._lanes.get(source)
            if lane is None:
                return
            limit = _number(lowered.get("x-rate-limit-limit"))
            window = _seconds(lowered.get("x-rate-limit-interval"))
            if limit and window:
                lane.learned_interval = window / limit
            most = _number(lowered.get("x-concurrency-limit"))
            if most:
                lane.learned_concurrency = int(most)
            usd = _number(lowered.get("x-ratelimit-remaining-usd"))
            if usd is not None:
                lane.remaining_usd = usd
        remaining = _number(lowered.get("x-ratelimit-remaining"))
        reset = _number(lowered.get("x-ratelimit-reset"))
        if remaining is not None and remaining <= 0 and reset:
            wait = reset - time.time() if reset > 1e9 else reset
            if wait > 0:
                kind = (
                    http.ALLOWANCE_USED if wait > http.ALLOWANCE_THRESHOLD_S else http.RATE_LIMITED
                )
                self._limits.cool(source, wait, kind, "provider says none left")

    def view(self, source: str, terms: Terms | None = None) -> dict[str, Any]:
        """Queue and allowance state of ``source`` for Settings.

        ``terms`` are the tier's terms now (the lane's last terms when omitted), so a
        source CLIO has not called yet still shows its allowance.
        """

        now = time.time()
        with self._lock:
            lane = self._lanes.get(source)
            current = terms or (lane.terms if lane else None)
            usage = lane.usage if lane is not None and lane.terms == current else _Usage()
            usage.roll(now)
            used: dict[str, Any] | None = None
            allowance = current.allowance() if current else None
            if allowance is not None:
                limit, unit, period = allowance
                spent: float = (
                    usage.day_usd
                    if unit == "usd"
                    else usage.day_requests
                    if period == "day"
                    else usage.hour_requests
                )
                used = {"spent": round(spent, 4), "limit": limit, "unit": unit, "per": period}
                if unit == "usd" and lane is not None and lane.remaining_usd is not None:
                    used["provider_remaining"] = lane.remaining_usd
            return {
                "waiting": sum(len(line) for line in lane.lines.values()) if lane else 0,
                "running": lane.active if lane else 0,
                "used": used,
            }

    def reset(self) -> None:
        """Forget every queue (tests, and a person resetting search)."""

        with self._lock:
            self._lanes.clear()

    # --------------------------------------------------------------- queueing
    def _enqueue(self, source: str, terms: Terms, caller: str) -> int:
        ticket = next(self._tickets)
        with self._lock:
            lane = self._lanes.setdefault(source, _Lane())
            if lane.tier != terms.tier:
                lane.tier, lane.terms, lane.shrink = terms.tier, terms, 0
                lane.learned_interval = lane.learned_concurrency = None
                lane.remaining_usd = None
            lane.lines.setdefault(caller, deque()).append(ticket)
        return ticket

    def _requeue(self, source: str, ticket: int, caller: str) -> None:
        with self._lock:
            lane = self._lanes[source]
            line = lane.lines.setdefault(caller, deque())
            if ticket not in line:
                line.appendleft(ticket)
            lane.lines.move_to_end(caller, last=False)

    def _drop(self, source: str, ticket: int, caller: str) -> None:
        with self._lock:
            lane = self._lanes.get(source)
            line = lane.lines.get(caller) if lane else None
            if line is not None and ticket in line:
                line.remove(ticket)
            if lane is not None and line is not None and not line:
                lane.lines.pop(caller, None)

    async def _turn(
        self, source: str, ticket: int, caller: str, deadline: float, cost_usd: float
    ) -> None:
        """Wait until this ticket may run; raise when it cannot within the deadline."""

        while True:
            self._check_allowance(source, cost_usd)
            resting = self._limits.cooldown(source)
            now = time.monotonic()
            if resting is not None:
                left = resting.until - time.time()
                if now + left > deadline:
                    raise SourceError(source, resting.kind, resting.detail, left)
                await asyncio.sleep(min(left, deadline - now, 1.0))
                continue
            with self._lock:
                lane = self._lanes[source]
                ready = (
                    lane.next_ticket() == ticket
                    and lane.active < lane.concurrency()
                    and lane.next_start <= now
                )
                if ready:
                    line = lane.lines[caller]
                    line.popleft()
                    lane.lines.move_to_end(caller)  # the next caller's turn
                    if not line:
                        lane.lines.pop(caller)
                    lane.active += 1
                    lane.next_start = now + lane.interval()
                    lane.usage.roll(time.time())
                    lane.usage.hour_requests += 1
                    lane.usage.day_requests += 1
                    lane.usage.day_usd += cost_usd
                    return
                wait = max(lane.next_start - now, _POLL_S)
            if now + wait > deadline:
                raise QueueBusy(source, http.RATE_LIMITED, BUSY_DETAIL)
            await asyncio.sleep(min(wait, 0.5))

    def _check_allowance(self, source: str, cost_usd: float) -> None:
        """Refuse before calling when this CLIO's free allowance is used up."""

        now = time.time()
        with self._lock:
            lane = self._lanes[source]
            terms = lane.terms
            lane.usage.roll(now)
            used_up = ""
            reset_s = 0.0
            if terms is not None and terms.usd_per_day is not None and cost_usd > 0:
                left = terms.usd_per_day - lane.usage.day_usd
                if lane.remaining_usd is not None:
                    left = min(left, lane.remaining_usd)
                if left < cost_usd:
                    used_up, reset_s = "today's free budget is spent", _to_next_day(now)
            elif terms is not None and terms.per_day is not None:
                if lane.usage.day_requests >= terms.per_day:
                    used_up, reset_s = "today's free requests are used", _to_next_day(now)
            elif terms is not None and terms.per_hour is not None:
                if lane.usage.hour_requests >= terms.per_hour:
                    used_up, reset_s = "this hour's free requests are used", _to_next_hour(now)
        if used_up:
            self._limits.cool(source, reset_s, http.ALLOWANCE_USED, used_up)
            raise SourceError(source, http.ALLOWANCE_USED, used_up, reset_s)

    # ---------------------------------------------------------------- results
    def _finish(self, source: str) -> None:
        with self._lock:
            lane = self._lanes[source]
            lane.active = max(0, lane.active - 1)

    def _settle_answer(self, source: str) -> None:
        with self._lock:
            lane = self._lanes[source]
            lane.pushbacks_in_row = 0
            lane.answered_in_row += 1
            if lane.shrink and lane.answered_in_row >= RECOVER_AFTER:
                lane.shrink -= 1
                lane.answered_in_row = 0
        self._limits.answered(source)

    def _settle_failure(
        self,
        source: str,
        exc: SourceError,
        failed_once: bool,
        report: CallEvents,
        deadline: float,
    ) -> str:
        """Record a failure; return ``"requeue"``, ``"retry"`` or ``"raise"``."""

        if exc.kind == http.RATE_LIMITED:
            with self._lock:
                lane = self._lanes[source]
                lane.answered_in_row = 0
                lane.pushbacks_in_row += 1
                lane.shrink = min(lane.shrink + 1, lane.base_concurrency() - 1)
                first, most = BACKOFF_S
                backoff = min(most, first * 2 ** (lane.pushbacks_in_row - 1))
            pause = exc.retry_after_s if exc.retry_after_s is not None else backoff
            self._limits.cool(source, pause, exc.kind, exc.detail)
            if report.requeued < MAX_REQUEUES and time.monotonic() + pause <= deadline:
                report.requeued += 1
                return "requeue"
            return "raise"
        if exc.kind == http.FAILED and not failed_once:
            report.retried.append(exc.detail)
            return "retry"
        if exc.kind == http.KEY_REJECTED:
            self._limits.failed(source, exc.kind, exc.detail)
            self._limits.cool(source, 600, http.FAILED, "key rejected")
            return "raise"
        rest = exc.cooldown_s()
        if exc.kind == http.FAILED and exc.retry_after_s is None:
            first, repeated = FAILED_REST_S
            rest = repeated if self._limits.failures_in_row(source) else first
        self._limits.cool(source, rest, exc.kind, exc.detail)
        return "raise"


#: A wait in line shorter than this is not worth a note.
NOTE_WAIT_S = 2.0


def queue_notes(source: str, events: CallEvents) -> list[SearchNote]:
    """The typed notes for what the queue did to one request."""

    notes = [note("search_source_retried", source, detail) for detail in events.retried]
    if events.waited_s >= NOTE_WAIT_S:
        notes.append(note("search_source_waited", source, f"{events.waited_s:.0f}s in line"))
    return notes


def caller_id() -> str:
    """The session making the current tool call (the queue's unit of fairness)."""

    from clio_agent.tools.servers.shell_spill_store import (  # noqa: PLC0415 - leaf tools
        active_session_id,
    )

    try:
        return active_session_id()
    except (ImportError, LookupError, RuntimeError):
        return ""


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _seconds(value: str | None) -> float | None:
    """``"1s"``, ``"60s"``, ``"1m"`` or a bare number of seconds."""

    if not value:
        return None
    text = value.strip().lower()
    scale = {"s": 1.0, "m": 60.0, "h": 3600.0}.get(text[-1:], None)
    number = _number(text[:-1] if scale else text)
    return None if number is None else number * (scale or 1.0)


def _to_next_day(now: float) -> float:
    return 86400.0 - (now % 86400.0)


def _to_next_hour(now: float) -> float:
    return 3600.0 - (now % 3600.0)


#: The one scheduler for this process.
SCHEDULER = ProviderScheduler()

__all__ = [
    "LITERATURE_PATIENCE_S",
    "SCHEDULER",
    "WEB_PATIENCE_S",
    "CallEvents",
    "caller_id",
    "queue_notes",
    "ProviderScheduler",
    "QueueBusy",
]
