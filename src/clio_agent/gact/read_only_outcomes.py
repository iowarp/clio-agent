"""A read-only MCP call's record is written behind its reply, in group commits.

A read-only call (:mod:`clio_agent.gact.external_read_only`) is sent before anything is
recorded. Its one record (admission and settled outcome) used to be awaited before the
reply went back, so each call held its ``tools.per_server_concurrency`` slot until
about 13 clio-core round trips were confirmed on the single history writer. On a P7
burst (2,000 ``get_record`` calls, 8 in flight) calls started 61 ms apart and took a
median 0.48 s, for a server that answers in milliseconds (2026-11-03).

:class:`ReadOnlyOutcomes` queues the record on the history writer and returns at once:

* **Order.** Records go through the writer's FIFO queue
  (:meth:`~clio_agent.gact.core_io.CoreIO.submit_batch`), so every later operation on
  that writer -- task controls, turn retirement, the next call's own records -- runs
  after them, and one session's records keep their order.
* **Group commit.** Records queued while the writer is busy are written in one writer
  turn and confirmed together (:func:`~clio_agent.gact.task_history_group.run_grouped`):
  about 3 puts per group instead of 6 per call, and the call's freshly minted handle and
  reference are not looked up.
* **Barrier.** :meth:`ReadOnlyOutcomes.recorded` waits for every record queued so far.
  Readers that do not go through the writer call it first: the task tools, turn
  retirement and shutdown (:func:`outcomes_recorded`).
* **Failure is never silent.** A record that fails is logged with the typed reason
  ``read_only_outcome_deferred`` and handed to the existing recovery path
  (``CoreRecovery.defer``), which retries it alone after the writer's hold is
  reconciled. A retry that fails for any reason other than a recording hold is logged
  ``read_only_outcome_unrecorded`` and dropped: the call already answered, and after a
  crash an unrecorded read-only call is likewise reported as unknown, never resubmitted.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_history_group import GroupedOperation

__all__ = [
    "READ_ONLY_OUTCOME_DEFERRED",
    "READ_ONLY_OUTCOME_UNRECORDED",
    "ReadOnlyOutcomes",
    "outcomes_recorded",
]

logger = logging.getLogger(__name__)

#: Typed reason: a read-only call's record failed and was handed to recovery.
READ_ONLY_OUTCOME_DEFERRED = "read_only_outcome_deferred"
#: Typed reason: recovery could not record a read-only call's outcome; it is unknown.
READ_ONLY_OUTCOME_UNRECORDED = "read_only_outcome_unrecorded"


@dataclass
class _Outcome:
    identity: str
    write: Callable[[], Any]
    minted: tuple[str, ...]
    creates: Callable[[], Iterable[tuple[str, str]]] | None
    minted_scopes: tuple[tuple[str, str], ...]
    context: contextvars.Context
    loop: asyncio.AbstractEventLoop
    done: asyncio.Future[None]


def _settle(done: asyncio.Future[None], ok: bool, value: Any) -> None:
    if done.done():
        return
    if ok:
        done.set_result(None)
    else:
        done.set_exception(value)


class ReadOnlyOutcomes:
    """Write read-only calls' records behind their replies (module docstring).

    Args:
        history: The task history whose writer records them.
        recovery: The application's ``CoreRecovery`` (``app.state.task_recovery``).
    """

    def __init__(self, history: TaskHistory, recovery: Any) -> None:
        self.history = history
        self.recovery = recovery
        self._pending: set[asyncio.Future[None]] = set()

    def record(
        self,
        identity: str,
        write: Callable[[], Any],
        *,
        minted: tuple[str, ...] = (),
        creates: Callable[[], Iterable[tuple[str, str]]] | None = None,
        minted_scopes: tuple[tuple[str, str], ...] = (),
        recorded: Callable[[], None] | None = None,
    ) -> None:
        """Queue ``write`` on the history writer and return without waiting for it.

        Args:
            identity: The recovery identity of this record.
            write: The record, as it would run alone on the history writer.
            minted: Lookup names nothing can have recorded yet (the call's own handle
                and reference).
            creates: The ``(session, scope)`` content records it may create, probed
                together for its group (run on the writer).
            minted_scopes: Content records named after an identity just minted.
            recorded: Called on the event loop once the record is confirmed.
        """
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._pending.add(done)
        done.add_done_callback(self._pending.discard)
        context = contextvars.copy_context()
        item = _Outcome(identity, write, minted, creates, minted_scopes, context, loop, done)
        done.add_done_callback(lambda finished: self._finished(item, finished, recorded))
        try:
            self.history.io.submit_batch(self._run, item)
        except BaseException:  # the writer is closed: the caller gets the error, as before
            done.cancel()
            raise

    def _run(self, items: list[_Outcome]) -> None:
        """One writer turn: every queued record, confirmed together (on the writer)."""
        try:
            results = self.history.run_grouped(
                [
                    GroupedOperation(
                        item.write, item.minted, item.context, item.creates, item.minted_scopes
                    )
                    for item in items
                ]
            )
        except BaseException as error:  # noqa: BLE001 - forwards: each waiter, then recovery
            results = [(False, error)] * len(items)
        for item, (ok, value) in zip(items, results, strict=True):
            item.loop.call_soon_threadsafe(_settle, item.done, ok, value)

    def _finished(
        self,
        item: _Outcome,
        finished: asyncio.Future[None],
        recorded: Callable[[], None] | None,
    ) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is None:
            if recorded is not None:
                recorded()
            return
        logger.error(
            "Task boundary reason=%s identity=%s error_type=%s",
            READ_ONLY_OUTCOME_DEFERRED,
            item.identity,
            type(error).__name__,
            exc_info=error,
        )

        async def retry() -> bool:
            try:
                await self.history.io.run(item.write)
            except Exception as failure:
                if self.history.requires_recovery:
                    raise  # held: recovery restores the writer, then retries this
                logger.error(
                    "Task boundary reason=%s identity=%s error_type=%s",
                    READ_ONLY_OUTCOME_UNRECORDED,
                    item.identity,
                    type(failure).__name__,
                    exc_info=failure,
                )
                return True
            if recorded is not None:
                recorded()
            return True

        self.recovery.defer(item.identity, retry)

    @property
    def pending(self) -> int:
        """Records queued and not yet confirmed or handed to recovery."""
        return len(self._pending)

    async def recorded(self) -> None:
        """Wait until every record queued so far is confirmed or handed to recovery."""
        pending = tuple(self._pending)
        if pending:
            await asyncio.gather(
                *(asyncio.shield(done) for done in pending), return_exceptions=True
            )


async def outcomes_recorded(app: Any) -> None:
    """Wait for the application's queued read-only records (none: returns at once)."""
    external = getattr(getattr(app, "state", None), "external_tasks", None)
    outcomes = getattr(external, "outcomes", None)
    if isinstance(outcomes, ReadOnlyOutcomes):
        await outcomes.recorded()
