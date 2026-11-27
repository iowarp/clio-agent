"""iowarp-core 2.5's synchronous blob calls, as an opt-in for CLIO's single-blob ops.

iowarp-core 2.5 binds ``Client.PutBlob``, ``Client.GetBlob`` and ``Client.GetBlobSize``
(``core_bindings.cc:746-779``). They release the GIL for the whole call and, when the
client is connected to a shared-memory cache (``Client.HasCache()``), are the only
calls that use it; without one they are ordinary task calls. CLIO's default path
issues the ``Async*`` form and waits in :func:`clio_agent.arc.clio_core_async_ops.await_future`.

``arc.clio_core.blob_calls`` (``CLIO_ARC_CLIO_CORE_BLOB_CALLS``) selects the form for
one-blob size probes, reads and writes:

* ``async`` (default): ``Async*`` plus CLIO's progress-based wait. A daemon that stops
  making progress ends the wait typed (``ClioCoreFutureTimeout``).
* ``sync``: the synchronous calls. **They carry no bound**: a daemon that stalls
  mid-call holds the calling thread until it answers. Batched puts and size probes
  (``store_put_many``, ``store_blob_sizes``) stay asynchronous either way.

The trade-off and the measured effect are in ``docs/design/clio-core-2.5-upgrade.md``.
"""

from __future__ import annotations

import functools
from typing import Any

from clio_agent import conf

ASYNC = "async"
SYNC = "sync"
_MODES = (ASYNC, SYNC)
#: GetBlobSize's published "no such blob" return code (the async form's).
_BLOB_NOT_FOUND = 1


@functools.cache
def mode() -> str:
    """The configured form, read once per process (a change applies on restart)."""
    raw = conf.resolve(
        "arc.clio_core.blob_calls",
        env="CLIO_ARC_CLIO_CORE_BLOB_CALLS",
        default=ASYNC,
        cast=conf.as_str,
    )
    value = raw.strip().lower()
    if value not in _MODES:
        raise ValueError(f"arc.clio_core.blob_calls must be one of {_MODES}, got {raw!r}")
    return value


def enabled(client: Any) -> bool:
    """Whether to use the synchronous form with ``client`` (it must have the calls)."""
    return mode() == SYNC and hasattr(client, "GetBlobSize")


def blob_size(client: Any, tag_id: Any, name: str) -> int:
    """The stored size of ``name`` (0 when it does not exist)."""
    size = client.GetBlobSize(tag_id, name)
    return 0 if size is None else int(size)


def read(client: Any, tag_id: Any, name: str, length: int, offset: int) -> bytes:
    """``length`` bytes of ``name`` from ``offset``.

    Raises:
        RuntimeError: The runtime refused the read (the synchronous form gives no code).
    """
    data = client.GetBlob(tag_id, name, length, offset)
    if data is None:
        raise RuntimeError(f"GetBlob operation failed for {name!r} (synchronous call)")
    return bytes(data)


def put(client: Any, tag_id: Any, name: str, data: bytes, offset: int) -> int:
    """Write ``data`` to ``name`` at ``offset``; the runtime's return code (0 = written)."""
    return int(client.PutBlob(tag_id, name, data, offset))


__all__ = ["ASYNC", "SYNC", "blob_size", "enabled", "mode", "put", "read"]
