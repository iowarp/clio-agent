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

import threading
import time
from collections.abc import Callable
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
        while not future.done():
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)
        return True

    return done_within


def await_future(future: Any, *, op_name: str) -> Any:
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
    from clio_agent.arc.daemon_progress import (  # noqa: PLC0415 - cycle
        max_wait_s,
        wait_while_progressing,
    )

    outcome = wait_while_progressing(
        _polled(future),
        slice_s=resolve_liveness_policy().stall_after_s + _WATCH_FIRST_MARGIN_S,
        op_name=op_name,
        ceiling_s=max_wait_s() + _WATCH_FIRST_MARGIN_S,
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
            raise RuntimeError(f"clio-core AsyncPutBlob({name!r}) returned code {code}")


def delete_blob(client: Any, tag_id: Any, name: str) -> bool:
    """Delete one blob via ``AsyncDelBlob``; ``True`` iff it existed (return code 0)."""
    future = await_future(client.AsyncDelBlob(tag_id, name), op_name="delete")
    return bool(future.wait(0) == 0)


def store_put(store: Any, kind: str, name: str, body: bytes) -> None:
    """Write one blob of ``store`` (retried per :func:`put_blob_with_retry`), GIL-free.

    Reads ``store._client`` and the tag id at call time, so a stall-ladder retry after a
    reconnect uses the fresh client and re-resolved tag.
    """
    from clio_agent.arc.clio_core_retry import put_blob_with_retry  # noqa: PLC0415

    put_blob_with_retry(AsyncPutTag(store._client, store._tag_ids.get(kind)), name, body)


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
    tag_id = store._tag_ids.get(kind)
    issued = [(name, data, client.AsyncPutBlob(tag_id, name, data, 0)) for name, data in blobs]
    lost: dict[str, RuntimeError] = {}
    for name, data, future in issued:
        try:
            code = await_future(future, op_name="put").wait(0)
            if code != 0:
                raise RuntimeError(f"clio-core AsyncPutBlob({name!r}) returned code {code}")
        except RuntimeError as refused:
            try:
                put_blob_with_retry(AsyncPutTag(client, tag_id), name, data, first_failure=refused)
            except RuntimeError as final:
                lost[name] = final
        else:
            note_put_success()
    return lost


def store_delete(store: Any, kind: str, name: str) -> bool:
    """Delete one blob of ``store`` (a missing one is a no-op), GIL-free."""
    return delete_blob(store._client, store._tag_ids.get(kind), name)


def rpc_answers(client: Any, cte: Any, sentinel: str) -> bool:
    """One GIL-free round trip (``AsyncTagQuery``): ``True`` when the runtime answered."""
    future = await_future(
        client.AsyncTagQuery(sentinel, 1, cte.PoolQuery.Dynamic()), op_name="health_probe"
    )
    return bool(future.wait(0) == 0)
