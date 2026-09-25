"""Bounding the claude_code streaming pool's resident CLI-process count.

Owner module (#775 no-accretion — carved out of
:mod:`clio_agent.providers.claude_code_sessions` rather than grown there) for
the levers that bound how many ``claude`` CLI subprocesses
:class:`~clio_agent.providers.claude_code_sessions.ClaudeStreamClientPool` can
have resident at once, now that the pool is keyed by GACT session id (S2, B1)
rather than ``(model, cwd, thinking, scope)``:

* **Idle reap** (:func:`session_idle_ttl_s`, :func:`sweep_idle_session_entries`,
  :func:`reap_idle_session_entry`) — a session's connection that has gone
  quiet (its last call finished and nothing new has come in) is reclaimed the
  next time ANY session wants a connection. Only ever touches entries
  :meth:`~claude_code_sessions._StreamClientEntry.idle_for` reports reapable
  (never mid-stream — a live-in-use connection is never pulled from under its
  own caller). Every entry is session-keyed now, so — unlike the pre-S2 design
  — there is no separately-protected "shared base entry": idle reap applies
  uniformly, and B1's own promise (reused across a session's turns) already
  keeps an ACTIVELY-used session's connection warm regardless of this TTL.
* **Concurrency cap** (:func:`max_concurrent_claude_processes`, wired into
  the pool's connect gate) — a resource BACKSTOP (computed runaway
  protection, like ``MAX_SPAWN_DEPTH`` — never a correctness rule): resident
  CLI count tracks actively-streaming SESSIONS directly (#1305's
  deterministic per-session connection release), so this cap only ever bites
  a genuine runaway fan-out. The idle reap cannot bound a genuinely ACTIVE
  fan-out (multiple sessions truly streaming at once): those connections are
  busy, not idle, by design. The cap makes an over-the-limit connect WAIT for
  a free slot rather than fail or degrade.
* **Connect-wait surfacing** (:func:`await_connect_slot`,
  :data:`CONNECT_WAIT_REASONS`) — unchanged from the pre-S2 design: a queued
  connect is typed, surfaced at an expanding cadence, and feeds the waiting
  session's LM-activity liveness bucket so the turn no-progress watchdog
  counts the queue as progress, never a stall.
* **Pre-connect bounding** (:func:`max_precede_connects`, B2) — how many
  session-open pre-connects (see that module's :meth:`ClaudeStreamClientPool
  .precede_connect`) may sit unclaimed at once. A pre-connected entry lives in
  the SAME ``pool._entries`` as a claimed one, so the idle reap above already
  evicts it under the identical lifetime rule; this cap only bounds how many
  may be simultaneously CONNECTING, so a burst of session creates can never
  queue ahead of a real turn's own connect for the shared slots above.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from clio_agent.providers.claude_code_sessions import (
        ClaudeStreamClientPool,
        _StreamClientEntry,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "CONNECT_WAIT_REASONS",
    "await_connect_slot",
    "connect_wait_payload",
    "log_config_change_reconnect",
    "log_dead_client_replaced",
    "log_precede_connect_failed",
    "log_precede_connect_skipped",
    "max_concurrent_claude_processes",
    "max_precede_connects",
    "precede_connect",
    "reap_idle_session_entry",
    "session_idle_ttl_s",
    "sweep_idle_session_entries",
]


def session_idle_ttl_s() -> float:
    """Idle TTL (seconds) for a pooled entry before the next ``entry_for`` sweep reaps it.

    Resolved via ``providers.claude_code.stream_idle_ttl_s`` /
    ``CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S`` (file → env → default 15.0s).
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return float(
        conf.resolve(
            "providers.claude_code.stream_idle_ttl_s",
            env="CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S",
            default=15.0,
            cast=conf.as_float,
        )
    )


def max_precede_connects() -> int:
    """B2: how many session-open pre-connects may sit unclaimed at once.

    Resolved via ``providers.claude_code.max_precede_connects`` /
    ``CLIO_CLAUDE_CODE_MAX_PRECEDE_CONNECTS`` (file → env → default 2). Kept
    well under :func:`max_concurrent_claude_processes`'s own default so a
    burst of session creates can never fill every connect slot with
    speculative work and make a real turn's own connect wait behind it. ``0``
    disables session-open pre-connect entirely (every session's first turn
    connects cold, as before B2 was wired to session open).
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return max(
        0,
        int(
            conf.resolve(
                "providers.claude_code.max_precede_connects",
                env="CLIO_CLAUDE_CODE_MAX_PRECEDE_CONNECTS",
                default=2.0,
                cast=conf.as_float,
            )
        ),
    )


def max_concurrent_claude_processes() -> int:
    """Process-wide BACKSTOP cap on CONCURRENTLY-CONNECTED ``claude`` CLI subprocesses.

    Resolved via ``providers.claude_code.max_concurrent_processes`` /
    ``CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES`` (file → env → default 4).
    Every pooled entry — one per GACT session, plus every pending session-open
    pre-connect — draws from the SAME N slots at connect time and releases its slot on
    disconnect, so the resident CLI-process count this process can ever hold
    is bounded by N regardless of how many sessions exist. A connect beyond
    the cap WAITS (surfaced, typed, expanding — :func:`await_connect_slot` —
    never fails/degrades) for a slot.

    See iowarp/clio-agent#1305 (2026-09-03 owner ruling) for the full history
    of why N=4 (not N=1) is the validated default.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return max(
        1,
        int(
            conf.resolve(
                "providers.claude_code.max_concurrent_processes",
                env="CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES",
                default=4.0,
                cast=conf.as_float,
            )
        ),
    )


# --------------------------------------------------------------------------- #
# Typed connect-wait surfacing catalog (no silent waiting -- #775 ground rule,
# #1305).
# --------------------------------------------------------------------------- #
CONNECT_WAIT_REASONS: dict[str, dict[str, Any]] = {
    "connect_slot_queued": {
        "category": "session_connect_wait",
        "description": (
            "A pooled entry's connect is queued behind the process-wide "
            "max_concurrent_claude_processes() backstop cap -- every slot is "
            "currently held by another actively-connecting/connected entry. "
            "The wait is UNBOUNDED and never fails or degrades (the cap's "
            "documented contract); each attempt is surfaced here (#1305) so a "
            "queued connect is never invisible dead air to either the SDK "
            "bridge's per-call timeout or the turn no-progress watchdog."
        ),
    },
}


def connect_wait_payload(*, attempt: int, elapsed_s: float, next_retry_s: float) -> dict[str, Any]:
    """Typed connect-wait payload (catalog style)."""
    definition = CONNECT_WAIT_REASONS["connect_slot_queued"]
    return {
        "reason": "connect_slot_queued",
        **definition,
        "waiting_on": "claude connect slot",
        "attempt": attempt,
        "elapsed_s": round(elapsed_s, 3),
        "next_retry_s": next_retry_s,
    }


# Surfacing cadence for a queued connect (#1305): mirrors
# :mod:`clio_agent.arc.rpc_liveness`'s per-attempt backoff shape.
_SURFACE_INITIAL_S = 1.0
_SURFACE_MAX_S = 30.0
_SURFACE_BACKOFF_FACTOR = 3.0


async def await_connect_slot(
    slots: "threading.Semaphore",
    *,
    session_id: str = "",
    reclaim_idle_slot: Callable[[], Any] | None = None,
    poll_interval_s: float = 0.2,
    abandon: "threading.Event | None" = None,
) -> bool:
    """Wait for a free process-wide connect slot -- surfaced, typed, liveness-fed.

    See the module docstring's "Concurrency cap" section for the contract.
    Returns ``True`` iff the caller now holds an acquired slot; ``False`` iff
    ``abandon`` was set while still queued (no slot held either way).
    """
    loop = asyncio.get_running_loop()
    start = time.monotonic()
    attempt = 0
    next_surface_at = 0.0  # surface immediately on the first queued attempt
    surface_gap = _SURFACE_INITIAL_S
    while True:
        acquired = await loop.run_in_executor(None, slots.acquire, True, poll_interval_s)
        if abandon is not None and abandon.is_set():
            if acquired:
                slots.release()  # phantom acquire: hand it right back, unused
            return False
        if acquired:
            return True
        attempt += 1
        if reclaim_idle_slot is not None:
            reclaim_idle_slot()
        elapsed = time.monotonic() - start
        from clio_agent.runtime.lm_activity import note_lm_activity_for  # noqa: PLC0415

        note_lm_activity_for(session_id)
        if elapsed >= next_surface_at:
            from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
                stream_audit,
                stream_audit_enabled,
            )

            if stream_audit_enabled():
                stream_audit(
                    "provider.connect_wait",
                    provider="claude_code_sdk",
                    transport="sdk",
                    session_id=session_id,
                    **connect_wait_payload(
                        attempt=attempt, elapsed_s=elapsed, next_retry_s=surface_gap
                    ),
                )
            next_surface_at = elapsed + surface_gap
            surface_gap = min(surface_gap * _SURFACE_BACKOFF_FACTOR, _SURFACE_MAX_S)


def sweep_idle_session_entries(
    pool: "ClaudeStreamClientPool", ttl_s: float | None = None
) -> list[tuple[str, "_StreamClientEntry"]]:
    """Pop every entry of ``pool`` idle >= ``ttl_s`` (default :func:`session_idle_ttl_s`).

    Returns the evicted ``(session_id, entry)`` pairs — teardown + the
    stateful-registry notification (:func:`reap_idle_session_entry`) happen
    OUTSIDE ``pool._guard``: popping first keeps a concurrent ``entry_for`` for
    the SAME session id from handing out an entry mid-teardown.
    """
    resolved_ttl = session_idle_ttl_s() if ttl_s is None else ttl_s
    evicted: list[tuple[str, Any]] = []
    with pool._guard:  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
        for session_id, entry in list(pool._entries.items()):  # noqa: SLF001
            idle = entry.idle_for()
            if idle is not None and idle >= resolved_ttl:
                evicted.append((session_id, entry))
        for session_id, _entry in evicted:
            del pool._entries[session_id]  # noqa: SLF001
    for _session_id, entry in evicted:
        entry._dead = True  # noqa: SLF001 - refuse a late connect outside the pool (F6b)
    return evicted


def reap_idle_session_entry(session_id: str, entry: "_StreamClientEntry") -> None:
    """Close ``entry`` (idle-reap) and flag its stateful-delta scope, if any."""
    from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
        _note_scope_provider_error,
        stream_audit,
        stream_audit_enabled,
        transport_failure_payload,
    )

    model = entry._model or ""  # noqa: SLF001
    cwd = entry._cwd  # noqa: SLF001
    thinking_key_ = entry._thinking_key  # noqa: SLF001
    scope = entry._last_scope  # noqa: SLF001
    entry.close_nonblocking()
    if stream_audit_enabled():
        stream_audit(
            "provider.transport_error",
            provider="claude_code_sdk",
            transport="sdk",
            model=model,
            **transport_failure_payload("idle_reaped", f"session={session_id!r} idle-reaped"),
        )
    _note_scope_provider_error(scope, model=model, cwd=cwd, thinking_key_=thinking_key_)


def log_config_change_reconnect(model: str | None, changed_fields: list[str]) -> None:
    """B13/B4: typed log + audit row for a thinking/system_prompt/cwd-driven reconnect."""
    from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
        stream_audit,
        stream_audit_enabled,
        transport_failure_payload,
    )

    logger.info(
        "claude_code sdk pool: reason=config_change_requires_restart fields=%s model=%s",
        ",".join(changed_fields),
        model,
    )
    if stream_audit_enabled():
        stream_audit(
            "provider.transport_error",
            provider="claude_code_sdk",
            transport="sdk",
            model=model or "",
            **transport_failure_payload("config_change_requires_restart", ",".join(changed_fields)),
        )


def log_dead_client_replaced(session_id: str) -> None:
    """B17: typed log + audit row when a session's dead entry is replaced (fresh connect)."""
    from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
        stream_audit,
        stream_audit_enabled,
        transport_failure_payload,
    )

    logger.warning("claude_code sdk pool: reason=dead_client_replaced session=%s", session_id)
    if stream_audit_enabled():
        stream_audit(
            "provider.session_release",
            provider="claude_code_sdk",
            transport="sdk",
            session_id=session_id,
            **transport_failure_payload("dead_client_replaced"),
        )


def log_precede_connect_skipped(session_id: str) -> None:
    """B2: typed log when a session-open pre-connect is skipped at the pending cap."""
    logger.info(
        "claude_code sdk pool: reason=precede_connect_skipped session=%s "
        "cap=%d (max_precede_connects)",
        session_id,
        max_precede_connects(),
    )


def log_precede_connect_failed(session_id: str) -> None:
    """B2: typed log when a session-open pre-connect fails; never raises into the caller."""
    logger.warning(
        "claude_code sdk pool: reason=precede_connect_failed session=%s "
        "(the first turn connects normally)",
        session_id,
        exc_info=True,
    )


def precede_connect(
    pool: "ClaudeStreamClientPool",
    *,
    session_id: str,
    model: str | None = None,
    cwd: str | None = None,
    thinking: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> None:
    """B2: background-connect ``session_id``'s own entry on ``pool`` with its REAL config.

    Fire-and-forget -- NEVER blocks the caller and NEVER raises. A no-op when
    ``session_id`` already has an entry (already pre-connecting, already
    claimed, or already connected) or when :func:`max_precede_connects`
    pending pre-connects are already outstanding (typed,
    ``precede_connect_skipped`` -- a real turn's own connect must never queue
    behind speculative work). On failure the entry is left in place with no
    client (typed, ``precede_connect_failed``): the first real
    ``entry_for``/``stream`` for this session just connects it cold, exactly
    as if pre-connect had never run. Caller is
    :meth:`~claude_code_sessions.ClaudeStreamClientPool.precede_connect`, a
    thin delegator kept on the pool for gact's call site.
    """
    key = session_id or ""
    if not key:
        return
    from clio_agent.providers.claude_code_sessions import _StreamClientEntry  # noqa: PLC0415

    entry: _StreamClientEntry | None = None
    at_cap = False
    with pool._guard:  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
        if key not in pool._entries:  # noqa: SLF001
            if len(pool._precede_pending) >= max_precede_connects():  # noqa: SLF001
                at_cap = True
            else:
                entry = _StreamClientEntry(
                    connect_slots=pool._connect_slots,  # noqa: SLF001
                    reclaim_idle_slot=pool._reclaim_idle_for_slot,  # noqa: SLF001
                )
                pool._entries[key] = entry  # noqa: SLF001
                pool._precede_pending.add(key)  # noqa: SLF001
    if entry is None:
        if at_cap:
            log_precede_connect_skipped(key)
        return
    threading.Thread(
        target=_precede_connect_blocking,
        args=(pool, key, entry, model, cwd, thinking, system_prompt),
        daemon=True,
        name="claude-precede-connect",
    ).start()


def _precede_connect_blocking(
    pool: "ClaudeStreamClientPool",
    session_id: str,
    entry: "_StreamClientEntry",
    model: str | None,
    cwd: str | None,
    thinking: dict[str, Any] | None,
    system_prompt: str | None,
) -> None:
    """The background thread body for one :func:`precede_connect` call."""
    entry._ensure_loop()  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
    entry._mark_busy()  # noqa: SLF001 - never idle-reaped mid-connect
    try:
        fut = asyncio.run_coroutine_threadsafe(
            entry._ensure_client(  # noqa: SLF001
                pool.bump_construct,
                gact_session_id=session_id,
                model=model,
                cwd=cwd,
                thinking=thinking,
                system_prompt=system_prompt,
            ),
            entry._loop,  # noqa: SLF001
        )
        fut.result(timeout=60.0)
    except Exception:  # noqa: BLE001 - a failed pre-connect must never break the caller
        log_precede_connect_failed(session_id)
    finally:
        entry._mark_idle()  # noqa: SLF001
        with pool._guard:  # noqa: SLF001
            pool._precede_pending.discard(session_id)  # noqa: SLF001
