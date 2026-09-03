"""claude_code's non-blocking #1305 per-subagent release (#1305 review round).

Owner module (#775 no-accretion split out of
:mod:`clio_agent.providers.claude_code_sessions`, which sits at its 800-line
cap): the ABNORMAL-TERMINATION backstop half of #1305's deterministic
per-subagent connection release.

**Reframed (#1305 review, the structural finding).** A clean react-forward
exit ALREADY releases deterministically today: ``providers/stateful_common.py``'s
``stateful_scope`` binds a scope for the whole ``forward()`` call and its
``finally`` calls ``ClaudeStreamClientPool.release`` (via
``register_scope_registry``) the moment the loop returns — before this
module's :func:`release_session_resources_nonblocking` (dispatched from
``gact/task_fold.py``'s terminal-effects helper, once the AgentTask ALSO
reaches terminal) ever runs. So on the COMMON, healthy path this module finds
NOTHING to release — a clean no-op — and that is correct, not wasted work.

This module's actual job is the ABNORMAL paths where the scope's own
``finally`` never ran to completion, or a genuinely queued connect never
even reached the point where a scope would matter: a hard-cancelled child
turn, a crashed forward, or (the narrower #6b race below) a caller still
holding an entry from an EARLIER ``entry_for()`` after this module already
released it. It is the backstop that completes the lifecycle guarantee for
those paths — not the primary release mechanism.

**Non-blocking (F1).** Dispatched from the server's OWN event loop (the task
done-callback chain via ``gact/task_fold.py``): MUST NOT BLOCK.
``ClaudeStreamClientPool.release``'s ``close_blocking`` waits up to 15s per
entry (``fut.result(timeout=15)`` on the owner loop) — calling that here
would stall every other coroutine on the server loop for as long as it took.
:func:`release_session_resources_nonblocking` instead pops entries under the
pool's lock and hands each to ``entry.close_nonblocking()`` (fire-and-forget:
schedules the disconnect, stops the owner loop via a done-callback once it
actually completes, never blocks the caller).

**In-flight guard (F2a).** An entry genuinely mid-stream
(``entry.idle_for() is None``) is left completely untouched — ripping it out
would kill a live query. It stays in the pool for its own scope's clean-exit
teardown or the idle-TTL sweep to reclaim once it truly goes idle; a typed
``session_release_deferred_in_flight`` row is surfaced instead of a silent
skip.

**Orphaned-entry window (F6b).** A caller can hold an entry from
``entry_for()`` before this module's release lands (a genuine cross-thread
race: the release runs on the server loop, the caller may be on a different
executor thread). Each closed entry is marked ``_dead`` — a monotonic flag
:class:`~clio_agent.providers.claude_code_sessions._StreamClientEntry`
checks at the top of ``_ensure_client`` (and, round 3, at the very top of
``stream()`` too, before a loop thread is even minted): a late caller's
connect is REFUSED (typed, retryable — see :func:`dead_entry_error_message`)
rather than silently reconnecting a slot+CLI invisible to sweep/close. This
NARROWS the window (the flag is only checked at those points, not held
continuously) rather than closing it outright.

**Stateful-delta hazard (B2, round 3).** A popped entry may be a
delta-CAPABLE (engaged) scope-keyed connection —
:mod:`clio_agent.providers.claude_code_stateful`'s registry still thinks its
last-seen prefix is live on that now-dead subprocess. Exactly like
:func:`~clio_agent.providers.claude_code_stream_bounds.reap_idle_stream_entry`
already does for the idle-TTL path, every popped entry here ALSO flags
``stateful_registry().note_provider_error(...)`` so the next send on that
scope is forced to a full resend — never a delta tail shipped to a fresh
subprocess with no memory of the dropped prefix.
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
#: with ``lm.io_logging._TRANSIENT_PROVIDER_MARKERS`` — see that module).
DEAD_ENTRY_MARKER = "claude agent sdk entry released during a queued connect"


class StreamAbandonedError(Exception):
    """Internal sentinel (#1305 B1, round 3): the caller abandoned this
    stream while it was still queued for a connect slot (``await_connect_slot``
    returned ``False``). Never surfaced to a real caller -- nobody is
    listening by definition, since the caller only sets ``abandon`` from its
    own teardown path. Caught by ``_pump``'s own exception handling exactly
    like any other abnormal end (queues an "exc" entry nobody reads, then
    STREAM_END, then a no-op reset since no client was ever constructed).
    """


# --------------------------------------------------------------------------- #
# Typed release-deferral catalog (no silent skip -- #775 ground rule, F2a).
# Mirrors claude_code_stream_bounds.CONNECT_WAIT_REASONS' style/discipline: a
# deferred (in-flight) entry is NOT a failure, just a "not yet, someone else
# owns it" — typed and queryable rather than a silent no-op.
# --------------------------------------------------------------------------- #
SESSION_RELEASE_REASONS: dict[str, dict[str, Any]] = {
    "session_release_deferred_in_flight": {
        "category": "session_release_deferred",
        "description": (
            "A subagent's terminal-status release found its scope-keyed "
            "connection genuinely MID-STREAM (idle_for() is None) -- ripping "
            "it out now would kill an in-flight query. Left untouched for "
            "the scope's own clean-exit teardown (stateful_scope) or the "
            "idle-TTL sweep to reclaim once it actually goes idle."
        ),
    },
    "session_lifecycle_released": {
        "category": "session_lifecycle_release",
        "description": (
            "A subagent's scope-keyed connection was closed by the #1305 "
            "deterministic per-subagent release (an abnormal-termination "
            "backstop -- a clean exit already released via stateful_scope). "
            "The claude_code stateful delta registry is flagged provider_error "
            "so the next send on this scope is a full resend, never a delta "
            "shipped to a fresh subprocess with no memory of the dropped prefix."
        ),
    },
}


def dead_entry_error_message() -> str:
    """Typed, retryable message for a connect refused by a released entry (F6b).

    The LM retry layer classifies this transient via
    :data:`DEAD_ENTRY_MARKER` (kept in sync with
    ``lm.io_logging._TRANSIENT_PROVIDER_MARKERS``) and re-issues the call,
    which resolves through a fresh ``entry_for()`` that mints a brand-new
    (live) entry — resurrection is automatic.
    """
    return DEAD_ENTRY_MARKER


def release_session_resources_nonblocking(pool: "ClaudeStreamClientPool", session_id: str) -> None:
    """The claude_code #1305 release effect: non-blocking (F1), in-flight-safe (F2a).

    See this module's docstring for the full ABNORMAL-termination-backstop
    framing (and B2 for the stateful-delta hazard this also closes). Pops
    every scope-keyed entry ``entry_for`` recorded for ``session_id``
    (:func:`~clio_agent.providers.claude_code_stream_bounds.scopes_for_session`)
    that is NOT genuinely in flight, marks each ``_dead`` (F6b), tells the
    claude_code stateful delta registry the scope's session is gone (B2), and
    closes it non-blocking; an in-flight entry is left untouched (typed,
    surfaced, F2a). A session with nothing recorded (the common, clean-path
    case -- see module docstring) is a fast no-op.
    """
    from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
        stream_audit,
        stream_audit_enabled,
    )
    from clio_agent.providers.claude_code_stateful import stateful_registry  # noqa: PLC0415
    from clio_agent.providers.claude_code_stream_bounds import (  # noqa: PLC0415
        forget_scope_owner,
        scopes_for_session,
    )

    scopes = scopes_for_session(pool, session_id)
    if not scopes:
        return
    to_close: list[tuple[tuple[str, str | None, str | None, str], Any]] = []
    deferred = 0
    with pool._guard:  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
        for scope in scopes:
            keys = [key for key in pool._entries if key[3] == scope]  # noqa: SLF001
            for key in keys:
                entry = pool._entries[key]  # noqa: SLF001
                if entry.idle_for() is None:
                    deferred += 1
                    continue
                entry._dead = True  # noqa: SLF001 - refuse a late connect (F6b)
                del pool._entries[key]  # noqa: SLF001
                to_close.append((key, entry))
            forget_scope_owner(pool, scope)
    for key, entry in to_close:
        model, cwd, thinking_id, scope = key
        entry.close_nonblocking()
        # B2: the exact hazard reap_idle_stream_entry already documents and
        # handles for the idle-TTL path -- without this, the registry still
        # thinks its last-seen prefix is live on the now-dead subprocess, and
        # the next send on this scope would classify as a DELTA (append-only
        # tail) shipped to a fresh subprocess with no memory of the prefix.
        stateful_registry().note_provider_error((scope, model, cwd, thinking_id), scope)
        if stream_audit_enabled():
            stream_audit(
                "provider.transport_error",
                provider="claude_code_sdk",
                transport="sdk",
                model=model,
                reason="session_lifecycle_released",
                **SESSION_RELEASE_REASONS["session_lifecycle_released"],
            )
    if deferred and stream_audit_enabled():
        stream_audit(
            "provider.session_release",
            provider="claude_code_sdk",
            transport="sdk",
            session_id=session_id,
            reason="session_release_deferred_in_flight",
            deferred_count=deferred,
            **SESSION_RELEASE_REASONS["session_release_deferred_in_flight"],
        )
