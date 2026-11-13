"""Process-wide search state shared by every session: cooldowns, pacing, questions.

The GACT server is one process and child sessions run in it, so one table here
serves every parallel researcher. It records, per source (the request queue that
paces calls is :mod:`clio_agent.search.scheduler`):

* a **cooldown** after a rate limit, used-up allowance or failure, with the
  ``epoch`` it started (a new exhaustion is a new epoch, so a new question);
* **status** for Settings: last answered, last failure, limited until.

and, per decision **topic** (``approve:<source>``, ``exhausted:<source>:<epoch>``),
the one pending question many callers share: the first caller is the *leader*
and asks; the others wait for the answer. A leader holds a lease; if its call is
abandoned and the lease runs out, the next caller becomes leader and asks again.

All state is guarded by one ``threading.Lock`` (tool calls run on several threads
and event loops); waiting happens outside the lock.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

#: How long a leader's unanswered question blocks others from asking again.
QUESTION_LEASE_S = 20 * 60.0
#: How long a waiting caller polls for another caller's answer before giving up.
FOLLOWER_PATIENCE_S = 15 * 60.0
_POLL_S = 0.5


@dataclass
class Cooldown:
    """A source resting until ``until`` (epoch seconds) for ``kind``."""

    until: float
    kind: str
    detail: str
    epoch: int


@dataclass
class SourceStatus:
    """What Settings shows for one source (counts are for today, since CLIO started)."""

    last_answered: float = 0.0
    last_failure: float = 0.0
    last_failure_kind: str = ""
    last_failure_detail: str = ""
    day: str = ""
    answered_today: int = 0
    failed_today: int = 0

    def roll(self) -> None:
        """Start today's counts when the local date changed."""

        today = time.strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.answered_today, self.failed_today = today, 0, 0


@dataclass
class Topic:
    """One shared decision: a pending question or its answer."""

    leader: str = ""
    lease_until: float = 0.0
    answer: str = ""
    answered_at: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)


class SearchLimits:
    """The process-wide search table (see the module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cooldowns: dict[str, Cooldown] = {}
        self._status: dict[str, SourceStatus] = {}
        self._topics: dict[str, Topic] = {}
        self._declined: set[str] = set()

    # ------------------------------------------------------------ cooldowns
    def cooldown(self, source: str, now: float | None = None) -> Cooldown | None:
        """The source's active cooldown, or ``None`` when it may be called."""

        moment = time.time() if now is None else now
        with self._lock:
            current = self._cooldowns.get(source)
            if current is not None and current.until <= moment:
                del self._cooldowns[source]
                current = None
            return current

    def cool(self, source: str, seconds: float, kind: str, detail: str = "") -> Cooldown:
        """Rest ``source`` for ``seconds``; an active cooldown keeps its epoch."""

        now = time.time()
        with self._lock:
            current = self._cooldowns.get(source)
            epoch = current.epoch if current and current.until > now else int(now * 1000)
            until = max(now + max(seconds, 0.0), current.until if current else 0.0)
            cooled = Cooldown(until=until, kind=kind, detail=detail, epoch=epoch)
            self._cooldowns[source] = cooled
            status = self._status.setdefault(source, SourceStatus())
            status.roll()
            status.failed_today += 1
            status.last_failure = now
            status.last_failure_kind = kind
            status.last_failure_detail = detail
            return cooled

    def failed(self, source: str, kind: str, detail: str = "") -> None:
        """Record a failure that does not rest the source (e.g. a rejected key)."""

        with self._lock:
            status = self._status.setdefault(source, SourceStatus())
            status.roll()
            status.failed_today += 1
            status.last_failure = time.time()
            status.last_failure_kind = kind
            status.last_failure_detail = detail

    def answered(self, source: str) -> None:
        """Record a successful answer."""

        with self._lock:
            status = self._status.setdefault(source, SourceStatus())
            status.roll()
            status.answered_today += 1
            status.last_answered = time.time()

    def clear(self, source: str) -> None:
        """Forget a source's cooldown (e.g. the person added a key)."""

        with self._lock:
            self._cooldowns.pop(source, None)

    def status(self) -> dict[str, dict[str, Any]]:
        """Per-source status for Settings (only sources CLIO has seen)."""

        now = time.time()
        with self._lock:
            names = set(self._status) | set(self._cooldowns)
            rows: dict[str, dict[str, Any]] = {}
            for name in sorted(names):
                status = self._status.get(name, SourceStatus())
                status.roll()
                cooled = self._cooldowns.get(name)
                row: dict[str, Any] = {
                    "last_answered": status.last_answered or None,
                    "last_failure": status.last_failure or None,
                    "last_failure_kind": status.last_failure_kind or None,
                    "last_failure_detail": status.last_failure_detail or None,
                    "limited_until": None,
                    "limited_kind": None,
                    "answered_today": status.answered_today,
                    "failed_today": status.failed_today,
                }
                if cooled is not None and cooled.until > now:
                    row["limited_until"] = cooled.until
                    row["limited_kind"] = cooled.kind
                rows[name] = row
            return rows

    # ------------------------------------------------------------- questions
    def claim(self, topic: str, context: dict[str, Any] | None = None) -> tuple[str, str]:
        """Become leader of ``topic`` or learn who answers it.

        Returns ``("answer", value)`` when the topic is already decided,
        ``("lead", token)`` when this caller must ask, or ``("wait", "")`` when
        another caller's question is pending.
        """

        now = time.time()
        with self._lock:
            current = self._topics.get(topic)
            if current is not None and current.answer:
                return ("answer", current.answer)
            if current is not None and current.leader and current.lease_until > now:
                return ("wait", "")
            token = uuid.uuid4().hex
            self._topics[topic] = Topic(
                leader=token, lease_until=now + QUESTION_LEASE_S, context=dict(context or {})
            )
            return ("lead", token)

    def decide(self, topic: str, answer: str) -> None:
        """Record the person's answer for ``topic`` (every waiter sees it)."""

        with self._lock:
            current = self._topics.setdefault(topic, Topic())
            current.answer = answer
            current.answered_at = time.time()
            current.leader = ""

    def abandon(self, topic: str) -> None:
        """Release a topic whose question was cancelled, so it can be asked again."""

        with self._lock:
            current = self._topics.get(topic)
            if current is not None and not current.answer:
                del self._topics[topic]

    def answer_of(self, topic: str) -> str:
        """The recorded answer for ``topic`` (``""`` when undecided)."""

        with self._lock:
            current = self._topics.get(topic)
            return current.answer if current is not None else ""

    async def wait_for(self, topic: str, patience_s: float = FOLLOWER_PATIENCE_S) -> str:
        """Wait for another caller's answer; ``""`` when none came in time.

        Returns early with ``""`` when the leader's lease lapsed, so the caller
        can claim the topic and ask itself.
        """

        deadline = time.monotonic() + patience_s
        while time.monotonic() < deadline:
            with self._lock:
                current = self._topics.get(topic)
                if current is None:
                    return ""
                if current.answer:
                    return current.answer
                if current.lease_until <= time.time():
                    return ""
            await asyncio.sleep(_POLL_S)
        return ""

    # -------------------------------------------------------------- declines
    def decline(self, source: str) -> None:
        """The person declined ``source`` for this server's lifetime."""

        with self._lock:
            self._declined.add(source)

    def declined(self, source: str) -> bool:
        """Whether the person declined ``source`` since the server started."""

        with self._lock:
            return source in self._declined

    def reset(self) -> None:
        """Forget everything (tests, and a person resetting search in Settings)."""

        with self._lock:
            self._cooldowns.clear()
            self._status.clear()
            self._topics.clear()
            self._declined.clear()


#: The one table for this process.
SEARCH_LIMITS = SearchLimits()

__all__ = ["SEARCH_LIMITS", "Cooldown", "SearchLimits", "SourceStatus"]
