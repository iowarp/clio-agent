"""The loop-thread store guard (#1334): no ARC store write is waited on from a thread
that is running an asyncio event loop.

The GACT server serves every HTTP request, SSE stream, and heartbeat from ONE event-loop
thread. Every ARC store write is a synchronous clio-core RPC (~90 ms each, two native
calls); a write issued from the loop thread parks that thread in ``threading.wait`` for
the RPC's duration and freezes the whole server. The live #1334 gate measured that as
1.7 s at turn start and 3.1 s at finalize per turn, and the judge freeze of #1333 was
the same mechanism with an LM call.

This module makes that a TYPED DEFECT instead of a convention:

* :func:`assert_store_write_off_loop` raises :class:`LoopThreadStoreWrite` when the
  calling thread has a running loop. It is called from the persist choke points every
  backend shares (``segments.SegmentStore._put_scope`` / scope drop, the working-set
  search-companion refresh) so the unit-test backend (``arc.live._MemoryStore``) exercises
  it, and from ``rpc_liveness.guarded_store_rpc`` / ``guard_store_op`` for the real store.
* :func:`audit_store_read_on_loop` records a loop-thread READ as a structured
  ``store.read_on_loop_thread`` audit row (the stream audit log + one warning per op)
  without raising: a read from the loop is the same stall class but breaking a ``GET`` is
  not the fix; the live gate reads the audit rows as evidence and each site gets moved.

The right way to write from a coroutine is the pattern
``turn_forward.py::_run_turn_setup_off_loop`` established: ``loop.run_in_executor`` with
``contextvars.copy_context()``; see ``gact.off_loop`` for the emit helpers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque

from clio_agent.errors import ClioError
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

STORE_WRITE_ON_LOOP_THREAD = "store_write_on_loop_thread"
STORE_READ_ON_LOOP_THREAD = "store_read_on_loop_thread"

_READ_WARNED: set[str] = set()
_READ_WARNED_LOCK = threading.Lock()

# The last guard hits (writes refused, reads audited), for the regression lock in
# tests and for ``doctor``-style inspection: ``(kind, op, scope_or_name, thread)``.
_HITS: deque[tuple[str, str, str, str]] = deque(maxlen=256)


def guard_hits() -> list[tuple[str, str, str, str]]:
    """The recorded guard hits, oldest first (``("write"|"read", op, scope, thread)``)."""

    return list(_HITS)


def reset_guard_hits() -> None:
    """Clear the recorded hits (a test's arrange step)."""

    _HITS.clear()


class LoopThreadStoreWrite(ClioError):
    """A store write was waited on from a thread running an asyncio loop.

    Raised at the persist seam. The semantic-event path catches it LOUDLY
    (``ARC-EVENTS FAILED to persist``: the event is lost, the reason is logged) and the
    ledger seams propagate it to the route (a 500 with this typed reason); either way the
    hit is recorded in :func:`guard_hits`, which the regression lock asserts empty.
    """

    def __init__(self, op: str, *, scope: str = "") -> None:
        self.op = op
        self.scope = scope
        thread = threading.current_thread().name
        super().__init__(
            f"ARC store write {op!r} (scope={scope!r}) waited on from loop thread {thread!r}; "
            "run it on the turn executor (turn_forward._run_turn_setup_off_loop) or through "
            "gact.off_loop",
            error_type=STORE_WRITE_ON_LOOP_THREAD,
            details={"op": op, "scope": scope, "thread": thread},
        )


def _running_loop_here() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def assert_store_write_off_loop(op: str, *, scope: str = "") -> None:
    """Raise :class:`LoopThreadStoreWrite` when this thread is running an event loop.

    Args:
        op: The store operation about to block (``"segments.put"``, ``"put"``, ...).
        scope: The segment scope / record name, for the error and the audit row.
    """

    if not _running_loop_here():
        return
    thread = threading.current_thread().name
    _HITS.append(("write", op, scope, thread))
    stream_audit(
        "store.write_on_loop_thread",
        op=op,
        scope=scope,
        thread=thread,
        reason=STORE_WRITE_ON_LOOP_THREAD,
    )
    raise LoopThreadStoreWrite(op, scope=scope)


def audit_store_read_on_loop(op: str, *, name: str = "") -> bool:
    """Record (never raise) a store read issued from a thread running an event loop.

    Returns ``True`` when the read was on a loop thread, so a caller can count it.
    """

    if not _running_loop_here():
        return False
    _HITS.append(("read", op, name, threading.current_thread().name))
    stream_audit(
        "store.read_on_loop_thread",
        op=op,
        name=name,
        thread=threading.current_thread().name,
        reason=STORE_READ_ON_LOOP_THREAD,
    )
    with _READ_WARNED_LOCK:
        first = op not in _READ_WARNED
        _READ_WARNED.add(op)
    if first:
        logger.warning(
            "ARC store read %r issued from loop thread %s (name=%r): a %s stall; "
            "move the caller off the loop (further reads of this op audit-only)",
            op,
            threading.current_thread().name,
            name,
            STORE_READ_ON_LOOP_THREAD,
        )
    return True


__all__ = [
    "STORE_READ_ON_LOOP_THREAD",
    "STORE_WRITE_ON_LOOP_THREAD",
    "LoopThreadStoreWrite",
    "assert_store_write_off_loop",
    "audit_store_read_on_loop",
    "guard_hits",
    "reset_guard_hits",
]
