"""The obligations lane: work Clio owes, resumed after startup without blocking anyone.

Startup brings up only what makes every later request correct and then reports
ready. Work left open when Clio last stopped -- interrupted assignments, accepted mail
not yet delivered, owed display repairs -- is an obligation: it resumes on its own, in
the background, while people already use Clio.

* **Per conversation.** The conversations with open work are read from the open records
  alone (:func:`~clio_agent.gact.conversation_recovery.open_work_sessions`). Only those
  conversations wait (:meth:`Obligations.holds`); every other conversation is admitted
  at once.
* **Oldest first, someone waiting first.** Conversations are recovered oldest first. A
  conversation someone sends a message to (:meth:`Obligations.wanted`) is recovered next
  and released at once.
* **Bounded.** After recovery a conversation is released to run its pending work through
  the turn runner; at most ``agent_tasks.obligation_concurrency`` released conversations run
  at a time, so a large backlog cannot flood memory or the model provider. Each one loads
  only its own working set.

The lane is one task owned by the task runtime. It adds no store: everything it reads is
the open-record index, and everything it changes goes through the conversation owner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from clio_agent.gact.conversation_recovery import open_work_sessions, restore_session

if TYPE_CHECKING:
    from clio_agent.gact.conversations import Conversations

logger = logging.getLogger(__name__)


def obligation_concurrency() -> int:
    """How many resumed conversations may run their owed work at once (at least 1).

    Config: ``agent_tasks.obligation_concurrency`` / ``CLIO_OBLIGATION_CONCURRENCY``
    (default ``4``).
    """
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "agent_tasks.obligation_concurrency",
        env="CLIO_OBLIGATION_CONCURRENCY",
        default=4,
        cast=conf.as_int,
    )
    return max(1, int(value))


class Obligations:
    """Recover and resume, conversation by conversation, the work left open at the last stop."""

    def __init__(self, conversations: Conversations, *, concurrency: int | None = None) -> None:
        self.conversations = conversations
        self._loop = asyncio.get_running_loop()
        self._queue: list[str] = []
        self._wanted: set[str] = set()
        self._wake = asyncio.Event()
        self._slots = asyncio.Semaphore(concurrency or obligation_concurrency())
        self._running: set[asyncio.Task[None]] = set()
        self.held: set[str] = set()  # not yet recovered and released
        self.recovered = 0

    async def hold(self) -> None:
        """Hold exactly the conversations with open work, then open admission for the rest."""
        conversations = self.conversations
        history = conversations.history
        order = await history.io.run(open_work_sessions, history)
        self.held.update(order)
        self._queue = list(order)
        conversations.obligations = self
        conversations._restoring = False

    def holds(self, session_id: str) -> bool:
        """Whether ``session_id`` still waits; if so, someone wants it, so it goes next."""
        if session_id not in self.held:
            return False
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._want, session_id)
        return True

    def _want(self, session_id: str) -> None:
        if session_id in self.held:
            self._wanted.add(session_id)
            self._wake.set()

    async def run(self) -> None:
        """Recover every held conversation, then release it within the concurrency bound."""
        conversations = self.conversations
        history = conversations.history
        from clio_agent.gact.conversation_recovery import (  # noqa: PLC0415
            _repair_workflow_references,
        )

        await history.io.run(_repair_workflow_references, conversations)
        await history.io.run(conversations.app.state.questions.restore)
        await conversations.app.state.question_events.restore()
        while self._queue and not conversations._closing:
            if history.requires_recovery:
                logger.warning(
                    "obligations paused reason=core_recovery_required remaining=%d",
                    len(self._queue),
                )
                conversations._restoring = True  # Core recovery reruns the whole restore.
                return
            session_id = self._next()
            await restore_session(conversations, session_id)
            self.recovered += 1
            await self._release(session_id)
        if self._running:
            await asyncio.wait(set(self._running))
        conversations.signal_pending()

    def _next(self) -> str:
        """The next conversation: one someone is waiting on, else the oldest."""
        for index, session_id in enumerate(self._queue):
            if session_id in self._wanted:
                self._wanted.discard(session_id)
                return self._queue.pop(index)
        return self._queue.pop(0)

    async def _release(self, session_id: str) -> None:
        """Let a recovered conversation run its owed work, within the concurrency bound."""
        conversations = self.conversations
        history = conversations.history
        owed = await history.io.run(history.pending_mail, session_id)
        if session_id in self._wanted or not owed:
            # Someone is waiting, or nothing is owed: no slot needed.
            self._wanted.discard(session_id)
            self.held.discard(session_id)
            conversations.runner.signal(session_id)
            return
        await self._slots.acquire()
        self.held.discard(session_id)
        task = asyncio.create_task(conversations.runner.run_pending(session_id))
        self._running.add(task)
        task.add_done_callback(lambda done: self._drained(session_id, done))

    def _drained(self, session_id: str, done: asyncio.Task[None]) -> None:
        self._running.discard(done)
        self._slots.release()
        if not done.cancelled() and done.exception() is not None:
            logger.error(
                "obligation drain failed reason=obligation_drain_failed session=%s error=%r",
                session_id,
                done.exception(),
            )

    def pending(self) -> Iterable[str]:
        """Conversations still waiting for recovery, in order."""
        return tuple(self._queue)

    def snapshot(self) -> dict[str, Any]:
        """Progress for diagnostics: recovered, queued and running counts."""
        return {
            "recovered": self.recovered,
            "queued": len(self._queue),
            "running": len(self._running),
        }
