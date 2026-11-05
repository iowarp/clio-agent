"""ClioCoreStore operations on clio-core's async API, so a stalled daemon cannot freeze Python.

The blocking ``clio_cte_core_ext`` calls hold the GIL for the whole RPC (measured
against a suspended daemon: ``Tag.PutBlob``, ``Tag.GetBlob``, ``Tag.GetBlobSize``,
``Client.DelBlob`` and ``Tag(name)`` let the main thread run 0-1 times in 3 s). The
per-RPC stall watch (:func:`clio_agent.arc.rpc_liveness.call_with_liveness`) runs each
call on a worker thread and waits with a bound, but a worker blocked in a GIL-holding
call keeps the watcher itself from waking, so the bound could not fire and the whole
server (event loop, health endpoint) froze with it.

The async methods submit with the GIL released (``core_bindings.cc``:
``AsyncPutBlob``, ``AsyncGetBlob``, ``AsyncGetBlobSize``, ``AsyncDelBlob``,
``AsyncTagQuery``; measured for puts and deletes: the main thread ran ~300 times in the
same 3 s) and are used for every put, get, size and delete. :func:`await_future` polls
``Future.done()`` (it returns at once) and sleeps in between, GIL released, so a stall
still ends typed (``rpc_stalled``); once a kind's tag id is cached these run inline on
the caller thread instead of the pooled stall-watch worker.

Still blocking: ``Tag(name)`` (GetOrCreateTag, cached here once per kind),
``Tag.GetContainedBlobs`` and ``SemanticSearch``; these keep the pooled stall watch.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from clio_agent.arc.native_threads import run_on_native_thread
from clio_agent.arc.rpc_liveness import resolve_liveness_policy

# ``Future.done()`` returns at once; the sleep between checks releases the GIL.
_POLL_S = 0.01
# How much later than the caller's stall watch this worker-side wait decides: the stall
# watch (rpc_liveness) owns the typed ladder (reconnect, retry, quarantine), so on a
# stall it must decide first; this wait only ends the abandoned worker, typed.
_WATCH_FIRST_MARGIN_S = 1.0
# GetBlobSize's published "no such blob" return code.
_BLOB_NOT_FOUND = 1


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


@functools.cache
def _first_poll_s() -> float:
    """The first sleep while an async RPC is pending (``arc.liveness.first_poll_s``).

    Read once per process: it is on every small RPC's path, and config reads are not free.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "arc.liveness.first_poll_s",
        env="CLIO_ARC_LIVENESS_FIRST_POLL_S",
        default=0.0001,
        cast=conf.as_float,
    )


@functools.cache
def _sample_after_s() -> float:
    """How long a pending RPC is polled before the daemon's progress is first sampled.

    ``arc.liveness.sample_after_s``. Locating the daemon and reading its CPU and I/O
    counters costs pidfile and ``/proc`` reads; taken after a 1 ms miss it ran on most
    puts while the daemon was busy (P7, 2026-11-02). A stall is still decided a whole
    ``arc.liveness.stall_after_s`` window after this baseline.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "arc.liveness.sample_after_s",
        env="CLIO_ARC_LIVENESS_SAMPLE_AFTER_S",
        default=0.1,
        cast=conf.as_float,
    )


def _polled(future: Any) -> Callable[[float], bool]:
    """``done_within`` for a native Future: block in ``wait`` with the GIL released.

    The native ``Future.wait(max_sec)`` blocks without the interpreter lock and returns
    when the task completes. Polling ``done()`` instead woke a pending put up to eight
    times (sleeps from ``arc.liveness.first_poll_s``, doubling to 10 ms), each wake
    retaking the lock from the threads doing the agent's own work: a script's nested
    call waited on those hand-offs (2026-11-05 profile). A future without ``wait`` (a
    test double) is polled as before.
    """

    def done_within(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        wait = getattr(future, "wait", None)
        if callable(wait) and timeout > 0:
            wait(timeout)
            if future.done():
                return True
        interval = min(_first_poll_s(), _POLL_S)
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
    # A native submission usually finishes within milliseconds, also while the daemon
    # is busy. Poll that long before resolving configuration and sampling daemon
    # processes. Pending work still enters the same health/progress policy; this is no
    # execution deadline or alternate backend.
    polled = _polled(future)
    if polled(_sample_after_s()):
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
                name = self._tag_name(kind)
                # A process-lived thread enters the runtime (arc.native_threads).
                tag_id = run_on_native_thread(lambda: self._cte.Tag(name).GetTagId())
                self._ids[kind] = tag_id
        return tag_id

    def cached(self, kind: str) -> bool:
        """Whether ``kind``'s id is known, so using it makes no blocking ``Tag()`` call."""
        with self._lock:
            return kind in self._ids

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


def store_put_if_absent(store: Any, kind: str, name: str, body: bytes) -> bool:
    """Create blob ``name`` of ``store`` with ``body`` only if it does not exist (GIL-free).

    One ``AsyncPutBlobIfAbsent`` (iowarp-core 2.4): clio-core decides atomically, so two
    writers of the same name never both create it. Returns ``True`` when this call
    created the blob, ``False`` when it already existed (``PUT_EXISTS_RC``; the stored
    bytes are left as they were).

    Raises:
        ClioCorePutRefused: The runtime refused the put with any other code.
    """
    import clio_cte_core_ext as cte  # noqa: PLC0415 - loaded with the client

    tag_id = store._tag_ids.get(kind)
    future = await_future(store._client.AsyncPutBlobIfAbsent(tag_id, name, body, 0), op_name="put")
    code = future.wait(0)
    if code == cte.PUT_EXISTS_RC:
        return False
    if code != 0:
        raise ClioCorePutRefused(name, code)
    return True


def delete_blob(client: Any, tag_id: Any, name: str) -> bool:
    """Delete one blob via ``AsyncDelBlob``; ``True`` iff it existed (return code 0)."""
    future = await_future(client.AsyncDelBlob(tag_id, name), op_name="delete")
    return bool(future.wait(0) == 0)


def store_blob_size(store: Any, kind: str, name: str) -> int:
    """A blob's stored size (0 when missing), asked GIL-free (``AsyncGetBlobSize``).

    Raises:
        RuntimeError: The runtime refused the probe with a code other than not-found.
    """
    tag_id = store._tag_ids.get(kind)
    future = await_future(store._client.AsyncGetBlobSize(tag_id, name), op_name="get")
    code = future.wait(0)
    if code == _BLOB_NOT_FOUND:
        return 0
    if code:
        raise RuntimeError(f"GetBlobSize operation failed (return code {code})")
    return int(future.result(0) or 0)


def store_blob_sizes(store: Any, kind: str, names: Sequence[str]) -> dict[str, int]:
    """Stored sizes of several blobs (0 when missing): every ``AsyncGetBlobSize`` is
    issued before any is awaited, so the probe costs about one round trip.

    Raises:
        RuntimeError: The runtime refused a probe with a code other than not-found.
    """
    tag_id = store._tag_ids.get(kind)
    issued = [(name, store._client.AsyncGetBlobSize(tag_id, name)) for name in names]
    sizes: dict[str, int] = {}
    for name, pending in issued:
        future = await_future(pending, op_name="get")
        code = future.wait(0)
        if code and code != _BLOB_NOT_FOUND:
            raise RuntimeError(f"GetBlobSize operation failed (return code {code})")
        sizes[name] = 0 if code else int(future.result(0) or 0)
    return sizes


def store_read(store: Any, kind: str, name: str, length: int, offset: int = 0) -> bytes:
    """``length`` bytes of a blob from ``offset``, read GIL-free (``AsyncGetBlob``).

    Raises:
        RuntimeError: The runtime refused the read (its return code), as the blocking
            ``Tag.GetBlob`` did.
    """
    tag_id = store._tag_ids.get(kind)
    future = await_future(store._client.AsyncGetBlob(tag_id, name, length, offset), op_name="get")
    code = future.wait(0)
    if code:
        raise RuntimeError(f"GetBlob operation failed (return code {code})")
    return bytes(future.result(0))


def store_put(store: Any, kind: str, name: str, body: bytes, offset: int = 0) -> None:
    """Write one blob of ``store`` at ``offset`` (retried per :func:`put_blob_with_retry`),
    GIL-free. A put past the blob's end grows it; nothing shrinks it.

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
        offset=offset,
        recover_capacity=partial(recover_capacity, store, tag_id, name, len(body)),
    )


def store_put_many(
    store: Any,
    kind: str,
    blobs: list[tuple[str, bytes]],
    offsets: dict[str, int] | None = None,
) -> dict[str, RuntimeError]:
    """Write several blobs of ``store`` concurrently: every ``AsyncPutBlob`` is issued
    before any is awaited, then each is awaited. A blob named in ``offsets`` is written at
    that offset (0 otherwise). A refused blob continues its own bounded retry
    (:func:`put_blob_with_retry`). Returns ``{name: final refusal}`` of the blobs
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
    at = offsets or {}
    issued = [
        (name, data, client.AsyncPutBlob(tag_id, name, data, at.get(name, 0)))
        for name, data in blobs
    ]
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
                    offset=at.get(name, 0),
                    first_failure=refused,
                    recover_capacity=partial(recover_capacity, store, tag_id, name, len(data)),
                )
            except RuntimeError as final:
                lost[name] = final
        else:
            note_put_success()
    return lost


def store_delete(store: Any, kind: str, name: str) -> bool:
    """Delete one blob of ``store`` (a missing one is a no-op), GIL-free."""
    return delete_blob(store._client, store._tag_ids.get(kind), name)


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
