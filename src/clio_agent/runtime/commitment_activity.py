"""Report deliberate waits to the existing per-session turn watchdog.

Shared task controls, Core RPCs, foreground native work and legacy MCP commitments
retain their own timeout and cancellation rules. Silence during those waits must not
trigger an unrelated turn deadline. Counts belong to the verified waiting
session, so activity in another conversation cannot keep a wedged turn alive.
This transient activity signal neither schedules work nor owns task state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_LOCK = threading.Lock()
# Per-session in-flight commitment-wait count. Keyed by GACT session id; the
# ``""`` key holds calls made off-turn (CLI/optimizer) or before a session is
# bound. A session's count is DROPPED once it drains to zero (mirrors
# lm_activity.note_lm_end) so this dict never grows past currently-open waits.
_INFLIGHT: dict[str, int] = {}
# Completion timestamps exist only while a turn watchdog observes that session.
# They are transient progress evidence, not task state or retained history.
_COMPLETIONS: dict[str, dict[object, float]] = {}


@contextmanager
def observe_completions(session_id: str) -> Iterator[Callable[[], float]]:
    """Observe actual wait completion across delayed watchdog polls, then discard it."""
    token = object()
    with _LOCK:
        _COMPLETIONS.setdefault(session_id, {})[token] = 0.0

    def latest() -> float:
        with _LOCK:
            return _COMPLETIONS.get(session_id, {}).get(token, 0.0)

    try:
        yield latest
    finally:
        with _LOCK:
            watchers = _COMPLETIONS.get(session_id, {})
            watchers.pop(token, None)
            if not watchers:
                _COMPLETIONS.pop(session_id, None)


def _active_session() -> str:
    """Resolve the GACT session that owns the current commitment wait.

    Deferred import: ``clio_agent.gact`` transitively imports ``tools``, so a
    module-level import would cycle. Off-turn callers (CLI, optimizer) have no
    session bound and fall to the unattributed ``""`` bucket.
    """
    try:
        from clio_agent.gact.context import active_session_id  # noqa: PLC0415

        return active_session_id() or ""
    except Exception:  # noqa: BLE001 - context unavailable off-turn -> unattributed
        return ""


def note_commitment_start(session_id: str | None = None) -> None:
    """Register one commitment wait as in flight for the active session."""
    key = _active_session() if session_id is None else session_id
    with _LOCK:
        _INFLIGHT[key] = _INFLIGHT.get(key, 0) + 1


def note_commitment_end(session_id: str | None = None) -> None:
    """Release one completed commitment wait for the active session.

    Drops the bucket once it drains to zero -- an idle session leaves no
    residual entry (the same no-unbounded-growth discipline as lm_activity).
    """
    key = _active_session() if session_id is None else session_id
    with _LOCK:
        remaining = _INFLIGHT.get(key, 0) - 1
        if remaining <= 0:
            _INFLIGHT.pop(key, None)
        else:
            _INFLIGHT[key] = remaining
        watchers = _COMPLETIONS.get(key, {})
        if watchers:
            completed_at = time.monotonic()
            for token in watchers:
                watchers[token] = completed_at


def note_progress(session_id: str) -> None:
    """Record work this session owns that publishes nothing: a running script's tool
    call, a range read served to it. The session's turn watchdog takes it as progress,
    so a script working through a large file is not stopped as silent; one that goes
    quiet for the whole window still is."""
    if not session_id:
        return
    with _LOCK:
        watchers = _COMPLETIONS.get(session_id, {})
        if watchers:
            now = time.monotonic()
            for token in watchers:
                watchers[token] = now


def commitment_wait_in_flight(session_id: str | None = None) -> bool:
    """True when a commitment wait is in flight and counts as turn progress.

    With ``session_id`` given, answers strictly for that session's bucket, so
    the no-progress watchdog attributes an in-flight wait only to the turn
    that owns it (#761 defect-2: a busy neighbor session must never keep a
    genuinely wedged session alive). With ``session_id=None`` (off-turn
    callers), falls back to global-any.
    """
    with _LOCK:
        if session_id is not None:
            return _INFLIGHT.get(session_id, 0) > 0
        return any(count > 0 for count in _INFLIGHT.values())


@contextmanager
def track(unbounded: bool, *, session_id: str | None = None) -> Iterator[None]:
    """Exclude an enabled deliberate wait from the unrelated turn watchdog.

    The caller retains its own explicit timeout and cancellation semantics.
    Shared controls pass their verified caller identity; legacy callers may
    resolve the active session. Capture it once so cleanup releases that same
    session even if a caller changes its runtime context while waiting.
    """
    if not unbounded:
        yield
        return
    key = _active_session() if session_id is None else session_id
    note_commitment_start(key)
    try:
        yield
    finally:
        note_commitment_end(key)
