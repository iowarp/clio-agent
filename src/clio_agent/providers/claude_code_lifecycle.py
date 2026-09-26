"""claude_code's non-blocking #1305 per-session release (S2 simplification).

Owner module (#775 no-accretion split out of
:mod:`clio_agent.providers.claude_code_sessions`) for the ABNORMAL-TERMINATION
backstop half of #1305's deterministic per-session connection release, PLUS
the small shared sentinels (:class:`StreamAbandonedError`,
:func:`dead_entry_error_message`) the pool's connect path raises.

**Reframed for S2 (B1).** Since the pool is now keyed directly by GACT
session id (not a react-loop scope token layered through a separate
ownership registry), this module's job collapsed to a single dict pop: no
scope<->session bookkeeping is needed to find "the entries this session
owns" — there is at most ONE entry per session id, and it IS the dict entry
at that key. What remains genuinely owned here is the same as before:

* **Non-blocking (F1).** Dispatched from the server's OWN event loop (the
  task done-callback chain via ``gact/task_fold.py``): MUST NOT BLOCK.
  ``ClaudeStreamClientPool.release``'s ``close_blocking`` waits up to 15s
  (``fut.result(timeout=15)`` on the owner loop) — calling that here would
  stall every other coroutine on the server loop. This module instead pops
  the entry under the pool's lock and hands it to
  ``entry.close_nonblocking()`` (fire-and-forget).
* **In-flight guard (F2a).** An entry genuinely mid-stream
  (``entry.idle_for() is None``) is left completely untouched — ripping it
  out would kill a live query. A typed ``session_release_deferred_in_flight``
  row is surfaced instead of a silent skip.
* **Orphaned-entry window (F6b).** A caller can hold an entry from
  ``entry_for()`` before this module's release lands (a genuine cross-thread
  race). The popped entry is marked ``_dead`` so a late caller's connect is
  REFUSED (typed, retryable) rather than silently reconnecting a slot+CLI
  invisible to the pool.
* **Stateful-delta hazard.** A popped entry may have an ACTIVE stateful-delta
  scope riding it (:mod:`claude_code_stateful`'s registry thinks its
  last-seen prefix is live on that now-dead subprocess) — flagged exactly
  like :func:`~clio_agent.providers.claude_code_stream_bounds.reap_idle_session_entry`
  already does for the idle-TTL path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool

__all__ = [
    "DEAD_ENTRY_MARKER",
    "SESSION_RELEASE_REASONS",
    "StreamAbandonedError",
    "dead_entry_error_message",
    "release_session_resources_nonblocking",
]

#: Marker substring the LM retry layer recognizes as transient (kept in sync
#: with ``lm.io_logging._TRANSIENT_PROVIDER_MARKERS``).
DEAD_ENTRY_MARKER = "claude agent sdk entry released during a queued connect"


class StreamAbandonedError(Exception):
    """Internal sentinel: the caller abandoned this stream while it was still
    queued for a connect slot (``await_connect_slot`` returned ``False``).
    Never surfaced to a real caller. Caught by ``_pump``'s own exception
    handling exactly like any other abnormal end.
    """


SESSION_RELEASE_REASONS: dict[str, dict[str, Any]] = {
    "session_release_deferred_in_flight": {
        "category": "session_release_deferred",
        "description": (
            "A session's terminal-status release found its connection genuinely "
            "MID-STREAM (idle_for() is None) -- ripping it out now would kill an "
            "in-flight query. Left untouched for the idle-TTL sweep to reclaim "
            "once it actually goes idle."
        ),
    },
    "session_lifecycle_released": {
        "category": "session_lifecycle_release",
        "description": (
            "A session's connection was closed by the #1305 deterministic "
            "per-session release (an abnormal-termination backstop). The "
            "claude_code stateful-delta registry is flagged provider_error, if "
            "an active scope was riding it, so the next send on that scope is a "
            "full resend, never a delta shipped to a fresh subprocess with no "
            "memory of the dropped prefix."
        ),
    },
}


def dead_entry_error_message() -> str:
    """Typed, retryable message for a connect refused by a released entry (F6b)."""
    return DEAD_ENTRY_MARKER


def release_session_resources_nonblocking(pool: "ClaudeStreamClientPool", session_id: str) -> None:
    """The claude_code #1305 release effect: non-blocking (F1), in-flight-safe (F2a).

    Pops the entry keyed to ``session_id`` (if any) that is NOT genuinely in
    flight, marks it ``_dead`` (F6b), tells the stateful-delta registry the
    session's connection is gone (if an active scope was riding it), and
    closes it non-blocking. A session with no entry is a fast no-op — the
    common, clean-path case (this backstop finds nothing because nothing
    abnormal happened).
    """
    from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
        _note_scope_provider_error,
        stream_audit,
        stream_audit_enabled,
    )

    with pool._guard:  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
        entry = pool._entries.get(session_id)  # noqa: SLF001
        if entry is None:
            return
        if entry.idle_for() is None:
            deferred_entry = entry
        else:
            entry._dead = True  # noqa: SLF001 - refuse a late connect (F6b)
            del pool._entries[session_id]  # noqa: SLF001
            deferred_entry = None
    if deferred_entry is not None:
        if stream_audit_enabled():
            stream_audit(
                "provider.session_release",
                provider="claude_code_sdk",
                transport="sdk",
                session_id=session_id,
                reason="session_release_deferred_in_flight",
                **SESSION_RELEASE_REASONS["session_release_deferred_in_flight"],
            )
        return
    model, cwd, thinking_key_, scope = (
        entry._model or "",
        entry._cwd,
        entry._thinking_key,
        entry._last_scope,
    )  # noqa: SLF001
    entry.close_nonblocking()
    _note_scope_provider_error(scope, model=model, cwd=cwd, thinking_key_=thinking_key_)
    if stream_audit_enabled():
        stream_audit(
            "provider.transport_error",
            provider="claude_code_sdk",
            transport="sdk",
            model=model,
            reason="session_lifecycle_released",
            **SESSION_RELEASE_REASONS["session_lifecycle_released"],
        )
