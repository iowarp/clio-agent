"""ClioCoreStore operations on clio-core's async API, so a stalled daemon cannot freeze Python.

The blocking ``clio_cte_core_ext`` calls hold the GIL for the whole RPC (measured
against a suspended daemon: ``Tag.PutBlob``, ``Tag.GetBlob``, ``Tag.GetBlobSize``,
``Client.DelBlob`` and ``Tag(name)`` let the main thread run 0-1 times in 3 s). The
per-RPC stall watch (:func:`clio_agent.arc.rpc_liveness.call_with_liveness`) runs each
call on a worker thread and waits with a bound, but a worker blocked in a GIL-holding
call keeps the watcher itself from waking, so the bound could not fire and the whole
server (event loop, health endpoint) froze with it.

The async methods that submit without holding the GIL are used instead wherever the
binding has one (measured: ``AsyncPutBlob``, ``AsyncDelBlob``, ``AsyncTagQuery`` let the
main thread run ~300 times in the same 3 s). The worker polls ``Future.done()`` (it
returns at once) and sleeps in between, GIL released, so the stall watch's own bound
fires on time and the ladder degrades typed (``rpc_stalled``) as designed.

Still blocking, because the binding has no GIL-free alternative (drafted upstream):
``Tag(name)`` (GetOrCreateTag, cached here once per kind), ``Tag.GetBlobSize`` and
``Tag.GetBlob`` (``AsyncGetBlob`` blocks while submitting), ``Tag.GetContainedBlobs``
(``AsyncBlobQuery`` returns no rows for these tags), and ``SemanticSearch``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from clio_agent.arc.rpc_liveness import resolve_liveness_policy

# ``Future.done()`` returns at once; the sleep between checks releases the GIL.
_POLL_S = 0.01
# How much later than the caller's stall watch this worker-side wait decides: the stall
# watch (rpc_liveness) owns the typed ladder (reconnect, retry, quarantine), so on a
# stall it must decide first; this wait only ends the abandoned worker, typed.
_WATCH_FIRST_MARGIN_S = 1.0


class ClioCoreFutureTimeout(TimeoutError):
    """An async clio-core RPC stopped progressing before it completed.

    A ``TimeoutError``, NOT a ``RuntimeError``: a pending RPC is not a refusal, so the
    write retry (:func:`~clio_agent.arc.clio_core_retry.put_blob_with_retry`) never
    re-issues a PutBlob that may still land nor records it as a lost write.

    Attributes:
        op_name: The RPC (``put`` / ``delete`` / ``health_probe``).
        reason: The wait's typed outcome (``no_progress`` / ``ceiling`` /
            ``daemon_pid_unresolved``; see :mod:`clio_agent.arc.daemon_progress`).
        waited_s: Seconds waited.
    """

    def __init__(self, op_name: str, reason: str, waited_s: float) -> None:
        self.op_name = op_name
        self.reason = reason
        self.waited_s = waited_s
        super().__init__(
            f"clio-core async {op_name} did not complete after {waited_s:.0f}s (reason={reason})"
        )


def _polled(future: Any) -> Callable[[float], bool]:
    """``done_within`` for a native Future: poll ``done()``, sleeping (GIL released)."""

    def done_within(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        interval = min(0.001, _POLL_S)
        while not future.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Local writes often finish within a millisecond. Charging the full
            # idle poll interval on every small RPC dominates large histories.
            # Back off to the existing cadence while a call actually stays pending.
            time.sleep(min(interval, remaining))
            interval = min(interval * 2, _POLL_S)
        return True

    return done_within


def await_future(future: Any, *, op_name: str, ceiling_s: float | None = None) -> Any:
    """Wait for ``future`` while the daemon keeps working; return it (done).

    No fixed deadline: a slow but working daemon is waited for (its CPU/I/O advancing;
    :func:`~clio_agent.arc.daemon_progress.wait_while_progressing`) up to the
    ``arc.liveness.max_wait_s`` ceiling. A whole ``arc.liveness.stall_after_s`` window
    with no daemon progress, the ceiling, or a daemon that cannot be located ends the
    wait typed -- each a margin later than the caller's stall watch, which owns the
    typed ladder and so decides first.

    Args:
        future: A ``clio_cte_core_ext.Future``.
        op_name: For the typed error.

    Raises:
        ClioCoreFutureTimeout: The future stopped progressing before it completed.
    """
    if future.done():  # the common case: no bound to resolve (config reads are not free)
        return future
    # A native submission can finish just after the first check. Give that
    # normal reply one millisecond before resolving configuration and sampling
    # daemon processes for every tiny write. Pending work still enters the same
    # health/progress policy; this is no execution deadline or alternate backend.
    polled = _polled(future)
    if polled(0.001):
        return future
    from clio_agent.arc.daemon_progress import (  # noqa: PLC0415 - cycle
        max_wait_s,
        wait_while_progressing,
    )

    ceiling = max_wait_s() + _WATCH_FIRST_MARGIN_S if ceiling_s is None else ceiling_s
    slice_s = resolve_liveness_policy().stall_after_s + _WATCH_FIRST_MARGIN_S
    if ceiling_s is not None:
        slice_s = min(slice_s, ceiling)
    outcome = wait_while_progressing(
        polled,
        slice_s=slice_s,
        op_name=op_name,
        ceiling_s=ceiling,
    )
    if not outcome.done:
        raise ClioCoreFutureTimeout(op_name, outcome.reason, outcome.waited_s)
    return future


class TagIds:
    """Per-kind CTE tag ids, resolved once per client (``Tag(name)`` blocks with the GIL).

    Tag ids are daemon state, so the cache is dropped whenever the store reconnects.
    """

    def __init__(self, cte: Any, tag_name: Any = str) -> None:
        self._cte = cte
        self._tag_name = tag_name  # kind -> the store's (namespaced) CTE tag name
        self._lock = threading.Lock()
        self._ids: dict[str, Any] = {}

    def get(self, kind: str) -> Any:
        """Return the tag id for ``kind``, creating the tag on first use."""
        # Resolve (create) under the lock: concurrent first writes to a NEW tag each
        # raced GetOrCreateTag outside it and stalled. Once per kind, then cached.
        with self._lock:
            tag_id = self._ids.get(kind)
            if tag_id is None:
                tag_id = self._cte.Tag(self._tag_name(kind)).GetTagId()
                self._ids[kind] = tag_id
        return tag_id

    def clear(self) -> None:
        """Forget every cached id (after a reconnect)."""
        with self._lock:
            self._ids.clear()

    def prewarm(self, kinds: tuple[str, ...]) -> None:
        """Resolve supported tag ids immediately after a healthy daemon attach."""
        for kind in kinds:
            self.get(kind)


class ClioCorePutRefused(RuntimeError):
    """A completed native put was refused, with its structured return code."""

    def __init__(self, name: str, code: int) -> None:
        self.native_code = code
        super().__init__(f"clio-core AsyncPutBlob({name!r}) returned code {code}")


class AsyncPutTag:
    """A ``Tag``-shaped writer over ``AsyncPutBlob`` (for :func:`put_blob_with_retry`).

    ``PutBlob`` raises ``RuntimeError`` on a non-zero return code, the same contract
    as the blocking ``Tag.PutBlob`` the retry helper was written for.
    """

    def __init__(self, client: Any, tag_id: Any) -> None:
        self._client = client
        self._tag_id = tag_id

    def PutBlob(self, name: str, data: bytes, off: int = 0) -> None:  # noqa: N802 - native shape
        """Write ``data`` to blob ``name`` and wait for the runtime's answer."""
        future = await_future(
            self._client.AsyncPutBlob(self._tag_id, name, data, off), op_name="put"
        )
        code = future.wait(0)
        if code != 0:
            raise ClioCorePutRefused(name, code)


def delete_blob(client: Any, tag_id: Any, name: str) -> bool:
    """Delete one blob via ``AsyncDelBlob``; ``True`` iff it existed (return code 0)."""
    future = await_future(client.AsyncDelBlob(tag_id, name), op_name="delete")
    return bool(future.wait(0) == 0)


def store_put(store: Any, kind: str, name: str, body: bytes) -> None:
    """Write one blob of ``store`` (retried per :func:`put_blob_with_retry`), GIL-free.

    Reads ``store._client`` and the tag id at call time, so a stall-ladder retry after a
    reconnect uses the fresh client and re-resolved tag.
    """
    from clio_agent.arc.clio_core_growth import recover_capacity  # noqa: PLC0415
    from clio_agent.arc.clio_core_retry import put_blob_with_retry  # noqa: PLC0415

    tag_id = store._tag_ids.get(kind)
    put_blob_with_retry(
        AsyncPutTag(store._client, tag_id),
        name,
        body,
        recover_capacity=partial(recover_capacity, store, tag_id, name, len(body)),
    )


def store_put_many(
    store: Any, kind: str, blobs: list[tuple[str, bytes]]
) -> dict[str, RuntimeError]:
    """Write several blobs of ``store`` concurrently: every ``AsyncPutBlob`` is issued
    before any is awaited, then each is awaited. A refused blob continues its own bounded
    retry (:func:`put_blob_with_retry`). Returns ``{name: final refusal}`` of the blobs
    that were lost (empty when all were written); every blob has completed by then.

    Raises:
        ClioCoreFutureTimeout: A put stopped progressing. It is NOT retried (the pending
            PutBlob may still land) and not recorded as lost: the stall is typed.
    """
    from clio_agent.arc.clio_core_retry import (  # noqa: PLC0415
        note_put_success,
        put_blob_with_retry,
    )

    client = store._client
    from clio_agent.arc.clio_core_growth import recover_capacity  # noqa: PLC0415

    tag_id = store._tag_ids.get(kind)
    issued = [(name, data, client.AsyncPutBlob(tag_id, name, data, 0)) for name, data in blobs]
    lost: dict[str, RuntimeError] = {}
    for name, data, future in issued:
        try:
            code = await_future(future, op_name="put").wait(0)
            if code != 0:
                raise ClioCorePutRefused(name, code)
        except RuntimeError as refused:
            try:
                put_blob_with_retry(
                    AsyncPutTag(client, tag_id),
                    name,
                    data,
                    first_failure=refused,
                    recover_capacity=partial(recover_capacity, store, tag_id, name, len(data)),
                )
            except RuntimeError as final:
                lost[name] = final
        else:
            note_put_success()
    return lost


#: How long a batch is polled as a whole before each pending reply gets the progress
#: watch of :func:`await_future` (a healthy daemon answers a window well within it).
_BATCH_POLL_S = 1.0
#: GetBlobSize's published "no such blob" code.
_NOT_FOUND = 1


def await_all(futures: Sequence[Any], *, op_name: str) -> None:
    """Wait for a batch of native futures together.

    Awaiting them one by one sleeps a growing interval, and samples the daemon's
    progress, for every reply that is not back yet. A batch is polled as a whole
    instead, once per millisecond, for up to :data:`_BATCH_POLL_S`; whatever is still
    pending after that is awaited with :func:`await_future`'s progress watch, so a
    stalled daemon still ends the wait typed.
    """
    pending = [future for future in futures if not future.done()]
    deadline = time.monotonic() + _BATCH_POLL_S
    while pending and time.monotonic() < deadline:
        time.sleep(0.001)  # releases the GIL
        pending = [future for future in pending if not future.done()]
    for future in pending:
        await_future(future, op_name=op_name)


def store_get_many(
    store: Any, kind: str, names: Sequence[str]
) -> tuple[dict[str, bytes | None], list[str]]:
    """Read several blobs of ``store`` concurrently: every size probe is issued, then
    every ``AsyncGetBlob`` of a blob that exists, each batch awaited together.

    Returns ``({name: record bytes}, stale)``: ``None`` for an absent blob, and in
    ``stale`` the names whose body read short of its own frame (size metadata lagging
    the write), for the caller to read again singly (:meth:`ClioCoreStore.get` re-reads
    the exact framed length).

    Raises:
        ClioCoreFutureTimeout: A read stopped progressing (the caller's stall watch
            decides first).
        RuntimeError: A size probe failed with a code other than "no such blob".
    """
    from clio_agent.arc.blob_frame import BlobFrameError, unframe  # noqa: PLC0415

    tag_id = store._tag_ids.get(kind)
    local = store._cte.PoolQuery.Local()
    probes = [(name, store._client.AsyncGetBlobSize(tag_id, name, local)) for name in names]
    await_all([future for _name, future in probes], op_name="get")
    reads = []
    for name, future in probes:
        code = future.wait(0)
        if code == _NOT_FOUND:
            continue
        if code != 0:
            raise RuntimeError(f"clio-core GetBlobSize failed for {name}: code {code}")
        size = int(future.result(0))
        if size > 0:
            reads.append((name, store._client.AsyncGetBlob(tag_id, name, size, 0)))
    await_all([future for _name, future in reads], op_name="get")
    out: dict[str, bytes | None] = dict.fromkeys(names)
    stale: list[str] = []
    for name, future in reads:
        try:
            out[name] = unframe(name, future.result())
        except BlobFrameError:
            stale.append(name)
    return out, stale


def store_delete(store: Any, kind: str, name: str) -> bool:
    """Delete one blob of ``store`` (a missing one is a no-op), GIL-free."""
    return delete_blob(store._client, store._tag_ids.get(kind), name)


def store_delete_together(store: Any, kind: str, names: Sequence[str]) -> None:
    """Delete a few blobs of ``store`` in one round trip (each missing one a no-op).

    Every ``AsyncDelBlob`` is issued, then each awaited; a stalled reply raises
    :class:`ClioCoreFutureTimeout` itself, typed for the caller's stall watch.
    """
    tag_id = store._tag_ids.get(kind)
    futures = [store._client.AsyncDelBlob(tag_id, name) for name in names]
    for future in futures:
        await_future(future, op_name="delete")


def store_delete_many(store: Any, kind: str, names: Sequence[str]) -> None:
    """Issue independent deletes together and await every submitted operation."""
    tag_id = store._tag_ids.get(kind)
    futures = [store._client.AsyncDelBlob(tag_id, name) for name in names]
    errors: list[Exception] = []
    for future in futures:
        try:
            await_future(future, op_name="delete").wait(0)
        except Exception as error:
            logging.getLogger(__name__).exception(
                "Core deletion failed reason=core_delete_observation_failed kind=%s", kind
            )
            errors.append(error)
    if errors:
        raise ExceptionGroup("Core deletion batch failed", errors)


def rpc_answers(client: Any, cte: Any, sentinel: str) -> bool:
    """One GIL-free round trip (``AsyncTagQuery``): ``True`` when the runtime answered."""
    from clio_agent.arc.rpc_liveness import health_probe_window_s  # noqa: PLC0415 - cycle

    future = await_future(
        client.AsyncTagQuery(sentinel, 1, cte.PoolQuery.Dynamic()),
        op_name="health_probe",
        ceiling_s=health_probe_window_s(),
    )
    return bool(future.wait(0) == 0)
