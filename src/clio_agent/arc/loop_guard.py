"""The SERVER-loop store guard (#1334): no ARC store write is waited on from the thread
that runs the GACT server's event loop.

The GACT server serves every HTTP request, SSE stream, and heartbeat from ONE event-loop
thread. Every ARC store write is a synchronous clio-core RPC (~90 ms each, two native
calls); a write issued from that thread parks it in ``threading.wait`` for the RPC's
duration and freezes the whole server. The live #1334 gate measured that as 1.7 s at turn
start and 3.1 s at finalize per turn, and the judge freeze of #1333 was the same mechanism
with an LM call.

WHICH loop is running matters, and that is the #1334 follow-up (live evidence, two legs):
the streamed LM path drives each provider call under its OWN ``asyncio.run`` loop on an
anyio worker thread (``lm/io_logging.py::_clio_streamed_call``), and the ``lm.call``
capture emits its semantic event from inside that private loop. A write there blocks only
that one worker — the server keeps answering — but a guard keyed on "any running loop"
refused it, and ``arc.memory.record_semantic_event`` catches the raise, so NINE semantic
events per run were DROPPED: worse than the stall the guard replaced. The guard therefore
keys on IDENTITY:

* :func:`register_server_loop` is called by the app's lifespan with the loop the server
  serves on (the same loop ``TurnRunner`` binds to). While at least one server loop is
  registered a write is refused ONLY on those loops; a write on any other running loop is
  a PRIVATE-loop write, audited (``store.write_on_private_loop``) and allowed.
* :func:`begin_server_loop_drain` marks a loop as tearing down (after the lifespan's
  ``yield``): a shutdown flush must LAND, so writes there are audited
  (``store.write_on_draining_loop``) and allowed — there are no requests left to freeze.
* With NO server loop registered (unit tests, the CLI, a bare ``asyncio.run``) the guard
  stays strict: any running loop refuses the write, so the regression locks keep catching
  real sites without a live server.

Surfaces:

* :func:`assert_store_write_off_loop` raises :class:`LoopThreadStoreWrite` per the rules
  above. It is called from the persist choke points every backend shares
  (``segments.SegmentStore._put_scope`` / scope drop, the working-set search-companion
  refresh) so the unit-test backend (``arc.live._MemoryStore``) exercises it, and from
  ``rpc_liveness.guarded_store_rpc`` / ``guard_store_op`` for the real store.
* :func:`audit_store_read_on_loop` records a loop-thread READ as a structured
  ``store.read_on_loop_thread`` audit row (the stream audit log + one warning per op)
  without raising: a read from the loop is the same stall class but breaking a ``GET`` is
  not the fix; the live gate reads the audit rows as evidence and each site gets moved.
* :func:`on_server_loop` lets a SYNC caller that cannot await (the ``lm.call`` capture)
  decide whether it must hand its write to ``gact.off_loop``.

The right way to write from a coroutine is the pattern
``turn_forward.py::_run_turn_setup_off_loop`` established: ``loop.run_in_executor`` with
``contextvars.copy_context()``; see ``gact.off_loop`` for the emit helpers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections import deque

from clio_agent.errors import ClioError
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

STORE_WRITE_ON_LOOP_THREAD = "store_write_on_loop_thread"
STORE_READ_ON_LOOP_THREAD = "store_read_on_loop_thread"
STORE_WRITE_ON_PRIVATE_LOOP = "store_write_on_private_loop"
STORE_WRITE_ON_DRAINING_LOOP = "store_write_on_draining_loop"

# The loops the GACT server serves requests on, and those in lifespan teardown. Weak so a
# closed loop (a finished TestClient app) can never keep a later, unrelated loop from
# being judged strictly. Guarded by ``_LOOPS_LOCK`` for the register/drain transitions.
_SERVER_LOOPS: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()
_DRAINING_LOOPS: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()
_LOOPS_LOCK = threading.Lock()

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


def register_server_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record ``loop`` as a loop the GACT server serves on (the app's lifespan calls it).

    Registering switches the guard from "any running loop" to IDENTITY: only writes on a
    registered loop are refused. Re-registering a draining loop revives it.
    """

    with _LOOPS_LOCK:
        _DRAINING_LOOPS.discard(loop)
        _SERVER_LOOPS.add(loop)


def begin_server_loop_drain(loop: asyncio.AbstractEventLoop) -> None:
    """Mark ``loop`` as tearing down: its writes are audited and ALLOWED from now on.

    Called right after the lifespan's ``yield``. The server answers no more requests on
    this loop, so a teardown flush (the turn drain, the trace close) must land rather than
    raise into a swallowing caller and lose the record.
    """

    with _LOOPS_LOCK:
        _SERVER_LOOPS.discard(loop)
        _DRAINING_LOOPS.add(loop)


def unregister_server_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Forget ``loop`` entirely (the lifespan's last act); the guard goes strict again."""

    with _LOOPS_LOCK:
        _SERVER_LOOPS.discard(loop)
        _DRAINING_LOOPS.discard(loop)


def _running_loop_here() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def on_server_loop() -> bool:
    """Whether THIS thread is currently running a registered (non-draining) server loop.

    The predicate for sync code that cannot await and must hand a store write to
    ``gact.off_loop`` instead (``lm/io_logging.py``'s ``lm.call`` capture).
    """

    loop = _running_loop_here()
    return loop is not None and loop in _SERVER_LOOPS


def _allowed_loop_reason(loop: asyncio.AbstractEventLoop) -> str:
    """The typed reason this running loop may block on a store write, or ``""``.

    ``""`` means REFUSE. A draining server loop and (once any server loop is registered)
    a private loop on a worker thread both block only themselves, never the server.
    """

    if loop in _DRAINING_LOOPS:
        return STORE_WRITE_ON_DRAINING_LOOP
    if loop in _SERVER_LOOPS:
        return ""
    # No server loop registered at all -> strict: treat this loop as the server's, so a
    # unit test / CLI run with no lifespan still catches a real write site.
    return STORE_WRITE_ON_PRIVATE_LOOP if len(_SERVER_LOOPS) else ""


def assert_store_write_off_loop(op: str, *, scope: str = "") -> None:
    """Raise :class:`LoopThreadStoreWrite` when this thread runs the SERVER's event loop.

    A write on a PRIVATE loop (an ``asyncio.run`` on a worker thread) or on a server loop
    already in lifespan teardown is audited with its typed reason and allowed: it blocks
    only its own thread, and refusing it drops the record instead (#1334 follow-up).

    Args:
        op: The store operation about to block (``"segments.put"``, ``"put"``, ...).
        scope: The segment scope / record name, for the error and the audit row.
    """

    loop = _running_loop_here()
    if loop is None:
        return
    thread = threading.current_thread().name
    allowed = _allowed_loop_reason(loop)
    if allowed:
        _HITS.append((allowed, op, scope, thread))
        stream_audit(
            f"store.{allowed.removeprefix('store_')}",
            op=op,
            scope=scope,
            thread=thread,
            reason=allowed,
        )
        return
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
    "STORE_WRITE_ON_DRAINING_LOOP",
    "STORE_WRITE_ON_LOOP_THREAD",
    "STORE_WRITE_ON_PRIVATE_LOOP",
    "LoopThreadStoreWrite",
    "assert_store_write_off_loop",
    "audit_store_read_on_loop",
    "begin_server_loop_drain",
    "guard_hits",
    "on_server_loop",
    "register_server_loop",
    "reset_guard_hits",
    "unregister_server_loop",
]
