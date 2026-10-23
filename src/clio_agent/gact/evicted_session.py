"""What this process drops when the resident transcript set evicts an idle session.

The resident set (:mod:`clio_agent.gact.resident_ledgers`, #889) bounds transcripts by
bytes, count and idle time. Two other in-process copies of the same session follow it:

* the A2UI projection cache, folded from the transcript the set just dropped;
* ARC's segment scopes. ARC writes every segment through to clio-core, which places
  it in its memory or disk tier, and reloads a scope from clio-core on its next access.
  Its in-process copy was never dropped, so a long-running server held every session
  it had served (#1846).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from clio_agent.gact.off_loop import schedule_off_loop
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI


def forget_evicted_session(app: "FastAPI", sid: str, *, is_active: Callable[[], bool]) -> None:
    """Drop the session's A2UI projection and release ARC's copy of it."""

    _forget_a2ui_projection(app, sid)
    _release_arc_session(app, sid, is_active)


def _forget_a2ui_projection(app: "FastAPI", sid: str) -> None:
    """Drop a just-evicted session's A2UI projection cache entry, if any.

    ``app.state.a2ui_store`` is always set before the resident set is built (see
    ``app.py``'s boot order), but stays defensive against a future reorder or a test
    harness that wires a bare ``ResidentLedgerSet`` without it.

    Calls ``forget_projection``, NOT ``forget_session`` (S8 review round 3, issue #1374
    item A, HIGH): eviction can fire for a session mid-write on ANOTHER thread, and
    ``forget_session`` also drops the session's write lock -- letting a concurrent second
    writer mint a fresh one and enter the same critical section. Only a genuine session
    DELETE may drop the lock; see ``A2UIStore.forget_projection``'s own docstring.
    """

    store = getattr(app.state, "a2ui_store", None)
    if store is not None:
        store.forget_projection(sid)


def _release_arc_session(app: "FastAPI", sid: str, is_active: Callable[[], bool]) -> None:
    """Release ARC's in-process copy of ``sid``; clio-core keeps the data.

    Release can wait on an in-flight invocation write, so it runs off the event loop. A
    session that turned active in the meantime is kept: the set evicts it again once it
    is idle. Each outcome is traced.
    """

    arc = getattr(app.state, "arc", None)
    if arc is None:
        return

    def release() -> None:
        if is_active():
            trace.event("RESIDENT-LEDGER", "arc_release_skipped_active session=%s", sid)
            return
        released = arc.release_session(sid)
        trace.event(
            "RESIDENT-LEDGER",
            "arc_released session=%s segments=%s inflight_pending=%s",
            sid,
            released.get("segments", 0),
            released.get("inflight_pending", 0),
        )

    schedule_off_loop(release, label="resident_ledger.arc_release")
