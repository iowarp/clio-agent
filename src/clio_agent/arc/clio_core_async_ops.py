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
from typing import Any

from clio_agent.arc.rpc_liveness import resolve_liveness_policy

# ``Future.done()`` returns at once; the sleep between checks releases the GIL.
_POLL_S = 0.01
# How far past the stall bound a worker keeps polling before it gives up: the stall
# watch has already abandoned it by then, so this only lets the worker thread end.
_GIVE_UP_MARGIN_S = 1.0


class ClioCoreFutureTimeout(RuntimeError):
    """An async clio-core RPC did not complete before its worker gave up polling."""


def await_future(future: Any, *, op_name: str, timeout_s: float | None = None) -> Any:
    """Poll ``future`` until it completes; return it (done). GIL released between polls.

    Args:
        future: A ``clio_cte_core_ext.Future``.
        op_name: For the timeout message.
        timeout_s: Give-up bound; defaults to the stall bound plus a margin, so the
            caller's stall watch (which fires at the stall bound) always decides first.

    Raises:
        ClioCoreFutureTimeout: The future was still pending at the give-up bound.
    """
    if future.done():  # the common case: no bound to resolve (config reads are not free)
        return future
    bound = (
        timeout_s
        if timeout_s is not None
        else (resolve_liveness_policy().stall_after_s + _GIVE_UP_MARGIN_S)
    )
    deadline = time.monotonic() + bound
    while not future.done():
        if time.monotonic() >= deadline:
            raise ClioCoreFutureTimeout(
                f"clio-core async {op_name} did not complete within {bound:g}s"
            )
        time.sleep(_POLL_S)
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


def store_delete(store: Any, kind: str, name: str) -> bool:
    """Delete one blob of ``store`` (a missing one is a no-op), GIL-free."""
    return delete_blob(store._client, store._tag_ids.get(kind), name)


def rpc_answers(client: Any, cte: Any, sentinel: str) -> bool:
    """One GIL-free round trip (``AsyncTagQuery``): ``True`` when the runtime answered."""
    future = await_future(
        client.AsyncTagQuery(sentinel, 1, cte.PoolQuery.Dynamic()), op_name="health_probe"
    )
    return bool(future.wait(0) == 0)
