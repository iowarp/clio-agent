"""Process-wide MCP commitment-wait activity tracker (iowarp/clio-agent#1230).

#1225 made ``wait_for_terminal`` an UNBOUNDED commitment at the MCP-call layer
(``AsyncMCPToolExecutor._timeout_budget_for_call`` returns ``seconds=None`` for
a declared commitment wait, so the per-call ceiling never fires against it).
But the GACT no-progress watchdog (``gact/turn_watchdog.py``,
``CLIO_GACT_TURN_TIMEOUT_S``) still bounds the whole TURN independently: a turn
legitimately blocked for hours inside one honest commitment wait — the relay's
own remote job runs, publishing nothing this session's bus listens for — goes
silent from the watchdog's point of view and is killed at the wall, the exact
defect class #1226 already removed one layer down (a turn's single tool call
being an unbounded commitment must complete, not die at a token/time ceiling).

This module is the missing signal: it mirrors ``runtime/lm_activity.py``'s
per-session in-flight tracker (same shape, same #761 defect-2 lesson — a busy
NEIGHBOR session must never keep a genuinely wedged session's watchdog alive)
so ``await_turn_work`` can ask "does THIS session have a commitment wait in
flight right now" and treat "yes" as progress, exactly like an actively
generating LM call. No per-call ceiling is layered on here: the MCP executor
already leaves the wait itself unbounded (#1225's whole point), so this
tracker never second-guesses that — it only reports whether one is open.

The instrumentation call sites live in ``tools/execution.py`` (start/end
around the unbounded foreground wait) via :func:`track`, a context manager
that is a plain no-op unless the call is a genuine unbounded commitment —
``tools/execution.py`` stays gact-agnostic (no import of ``gact`` at module
scope): the session is resolved here, deferred, exactly like
``lm_activity._active_lm_session``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_LOCK = threading.Lock()
# Per-session in-flight commitment-wait count. Keyed by GACT session id; the
# ``""`` key holds calls made off-turn (CLI/optimizer) or before a session is
# bound. A session's count is DROPPED once it drains to zero (mirrors
# lm_activity.note_lm_end) so this dict never grows past currently-open waits.
_INFLIGHT: dict[str, int] = {}


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


def note_commitment_start() -> None:
    """Register one commitment wait as in flight for the active session."""
    key = _active_session()
    with _LOCK:
        _INFLIGHT[key] = _INFLIGHT.get(key, 0) + 1


def note_commitment_end() -> None:
    """Release one completed commitment wait for the active session.

    Drops the bucket once it drains to zero -- an idle session leaves no
    residual entry (the same no-unbounded-growth discipline as lm_activity).
    """
    key = _active_session()
    with _LOCK:
        remaining = _INFLIGHT.get(key, 0) - 1
        if remaining <= 0:
            _INFLIGHT.pop(key, None)
        else:
            _INFLIGHT[key] = remaining


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
def track(unbounded: bool) -> Iterator[None]:
    """Scope a commitment wait's in-flight marker; a no-op unless ``unbounded``.

    Single unconditional call-site shape for ``tools/execution.py``: the
    caller always writes ``with commitment_activity.track(timeout is None):``
    regardless of whether THIS particular call is a genuine unbounded
    commitment, so the instrumentation never needs an ``if`` at the call site.
    """
    if not unbounded:
        yield
        return
    note_commitment_start()
    try:
        yield
    finally:
        note_commitment_end()
