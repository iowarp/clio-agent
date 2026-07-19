"""Kill-on-cancel registry for in-flight ``claude_code`` SDK streams (#993).

When a child turn is cancelled (the cancel cascade / a declared-workflow step stall),
its cooperative cancel flag stops the ReAct loop at the NEXT decision point — but the LM
call already in flight keeps running to completion. On the pooled Claude Agent SDK
transport that means the child's ``claude`` CLI subprocess keeps STREAMING after the task
is already cancelled: the settled transcript correctly refuses the late deltas
(``frozen_transcript_mirror``), but the flood is wasteful and noisy (239 typed
``late_op`` rejections + 19 empty end-handler errors observed live — sess_66643f9600a4).

This module is the seam that stops it: a stream, while actively generating, REGISTERS an
abort handle under the GACT session that owns it (:func:`register_sdk_stream`). Cancelling
that session (:func:`abort_session_streams`) fires the handle, which terminates ONLY that
stream's subprocess/connection — never the shared pool, never an unrelated session's
in-flight stream (the SDK client pool multiplexes distinct sessions onto one connection,
so the registration is scoped to whichever session currently OWNS the query).

The registry is transport-agnostic (the abort is an opaque callable), so the pooled
streaming entry wires in its own "reset this connection on its owner loop" abort while the
hermetic tests register a fake in-flight stream. The kill is announced with a typed
``cancelled_transport_killed`` reason (no silent teardown — the #775 no-silent-fallback
ground rule): a queryable log row, not an invisible side effect.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# Typed reason for the kill (no free-form strings on the wire — same discipline as
# claude_code_sessions.TRANSPORT_FAILURE_REASONS / gact.streaming._stream_fallback).
CANCELLED_TRANSPORT_KILLED = "cancelled_transport_killed"


class SdkStreamHandle:
    """An abortable in-flight SDK stream bound to one GACT ``session_id``.

    Created by :func:`register_sdk_stream` when a session's stream starts actively
    generating and dropped by :func:`unregister_sdk_stream` when it ends. :meth:`abort`
    is idempotent and one-shot: the FIRST call runs the transport's teardown (killing the
    CLI subprocess / aborting the stream) and returns ``True``; every later call is a
    no-op returning ``False``, so a cancel that races the stream's own natural end never
    double-tears-down.
    """

    __slots__ = ("session_id", "_abort", "_aborted", "_lock")

    def __init__(self, session_id: str, abort: Callable[[], None]) -> None:
        self.session_id = session_id
        self._abort = abort
        self._aborted = False
        self._lock = threading.Lock()

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted

    def abort(self) -> bool:
        """Terminate this stream exactly once. Returns whether THIS call did the kill."""

        with self._lock:
            if self._aborted:
                return False
            self._aborted = True
        try:
            self._abort()
        except Exception:  # noqa: BLE001 - a teardown fault must never break the cancel path
            logger.warning(
                "sdk stream abort callback failed reason=%s session=%s",
                CANCELLED_TRANSPORT_KILLED,
                self.session_id,
                exc_info=True,
            )
        return True


_LOCK = threading.RLock()
# GACT session_id -> the in-flight stream handles owning that session's query. A set (not a
# single handle) so a pathological re-entrant registration never silently drops one.
_STREAMS: dict[str, set[SdkStreamHandle]] = {}


def register_sdk_stream(session_id: str, abort: Callable[[], None]) -> SdkStreamHandle:
    """Register an in-flight SDK stream for ``session_id``; return its handle.

    ``abort`` is the transport's teardown for THIS stream (e.g. reset the pooled client's
    connection on its owner loop, killing the CLI subprocess). Off-turn callers with no
    session bound pass ``""`` — the handle is still returned (so the caller's
    register/unregister bookkeeping is uniform) but no cancel can ever target it.
    """

    handle = SdkStreamHandle(session_id, abort)
    if session_id:
        with _LOCK:
            _STREAMS.setdefault(session_id, set()).add(handle)
    return handle


def unregister_sdk_stream(handle: SdkStreamHandle) -> None:
    """Drop a handle when its stream ends (natural completion, error, or after abort)."""

    sid = handle.session_id
    if not sid:
        return
    with _LOCK:
        holders = _STREAMS.get(sid)
        if holders is None:
            return
        holders.discard(handle)
        if not holders:
            _STREAMS.pop(sid, None)


def abort_session_streams(session_id: str) -> int:
    """Kill every in-flight SDK stream owned by ``session_id``; return the count killed.

    Called from the child-task cancel primitive: a cancelled child's actively-generating
    SDK subprocess is terminated so it stops producing late ops. Handles for OTHER sessions
    are untouched — the pool multiplexes, but only the streams registered under this exact
    session id are aborted. Each kill emits the typed :data:`CANCELLED_TRANSPORT_KILLED`
    reason (no silent teardown). Returns 0 (no-op) when the session has no in-flight SDK
    stream — the common case for a non-``claude_code`` transport or an idle child.
    """

    if not session_id:
        return 0
    with _LOCK:
        holders = _STREAMS.pop(session_id, None)
    if not holders:
        return 0
    killed = 0
    for handle in list(holders):
        if handle.abort():
            killed += 1
    if killed:
        logger.warning(
            "sdk stream cancelled reason=%s session=%s streams_killed=%d",
            CANCELLED_TRANSPORT_KILLED,
            session_id,
            killed,
        )
    return killed


def active_stream_sessions() -> set[str]:
    """The GACT sessions with a registered in-flight SDK stream (diagnostics / tests)."""

    with _LOCK:
        return set(_STREAMS)


def _reset_for_tests() -> None:
    """Drop all registered streams (test isolation)."""

    with _LOCK:
        _STREAMS.clear()
