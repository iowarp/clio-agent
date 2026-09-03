"""Bounding the claude_code streaming pool's resident CLI-process count.

Owner module (#775 no-accretion — carved out of
:mod:`clio_agent.providers.claude_code_sessions` rather than grown there) for
the two levers that bound how many ``claude`` CLI subprocesses
:class:`~clio_agent.providers.claude_code_sessions.ClaudeStreamClientPool` can
have resident at once, without touching the AGENT-COPPER12 correctness
guarantee that :mod:`claude_code_sessions` itself owns (every ACTIVE stateful
scope gets its OWN isolated connection — a spawned child's delta must never
land on the parent's connection):

* **Idle reap** (:func:`stream_idle_ttl_s`, :func:`sweep_idle_scoped_entries`,
  :func:`reap_idle_stream_entry`) — a scope-keyed connection that has gone
  quiet (its own send finished, or a parent orchestrator is blocked in
  ``wait_agent_tasks`` while its own scope's connection just sits open) is
  reclaimed the next time a NEW scope wants a connection. Only ever touches
  entries :meth:`~claude_code_sessions._StreamClientEntry.idle_for` reports
  reapable (never mid-stream, never the shared base entry).
* **Concurrency cap** (:func:`max_concurrent_claude_processes`, wired into
  :class:`ClaudeStreamClientPool`'s connect gate) — a resource BACKSTOP
  (computed runaway protection, like ``MAX_SPAWN_DEPTH`` — never a
  correctness rule): with #1305's deterministic per-subagent connection
  release in place (``providers/session_lifecycle.py``), resident CLI count
  tracks actively-streaming agents directly, and this cap only ever bites a
  genuine runaway fan-out. The idle reap cannot bound a genuinely ACTIVE
  fan-out (multiple experts truly streaming at once, e.g.
  ``spawn_agents_parallel``): those connections are busy, not idle, by
  design. The cap makes an over-the-limit connect WAIT for a free slot
  rather than fail or degrade.
* **Connect-wait surfacing** (:func:`await_connect_slot`,
  :data:`CONNECT_WAIT_REASONS`) — the mechanism that makes queuing behind the
  cap SAFE rather than invisible dead air (#1305): a queued connect is typed,
  surfaced at an expanding cadence (mirrors
  :mod:`clio_agent.arc.rpc_liveness`'s per-attempt shape), and feeds the
  waiting session's LM-activity liveness bucket so the turn no-progress
  watchdog counts the queue as progress, never a stall. Root-caused live
  (iowarp/clio-agent#1305): pre-#1305, the queued wait silently burned the
  SDK bridge's 600s per-call timeout AND the 900s turn watchdog simultaneously
  — with N=1 (see :func:`max_concurrent_claude_processes`'s history below)
  any one long call starved every other queued session past both ceilings,
  deterministically.

Both original levers exist because the standard acceptance load's memory
budget (``scripts/mcp_mem_budget.json``) was recorded before e47285fb
(#COPPER12, "scope-keyed stream connections") — a spawned child's own
isolated connection is the CORRECT fix for a real cross-conversation bleed,
but it also means resource cost now scales with concurrently-active scopes
rather than staying pinned at one connection process-wide. Reaping the idle
case and bounding the active-concurrency case are how that correctness fix
stays inside the recorded budget.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import threading

    from clio_agent.providers.claude_code_sessions import (
        ClaudeStreamClientPool,
        _StreamClientEntry,
    )

__all__ = [
    "CONNECT_WAIT_REASONS",
    "await_connect_slot",
    "connect_wait_payload",
    "forget_scope_owner",
    "max_concurrent_claude_processes",
    "note_scope_owner",
    "reap_idle_stream_entry",
    "scopes_for_session",
    "stream_idle_ttl_s",
    "sweep_idle_scoped_entries",
]


def stream_idle_ttl_s() -> float:
    """Idle TTL (seconds) for a SCOPE-KEYED pooled entry before the next
    ``entry_for`` sweep reaps it (:func:`sweep_idle_scoped_entries`).

    Resolved via ``providers.claude_code.stream_idle_ttl_s`` /
    ``CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S`` (file → env → default 15.0s). The
    shared BASE entry (non-engaged sends, ``scope=""``) is never subject to
    this TTL — only isolated per-forward connections a spawned expert's own
    scope minted (#COPPER12 scope-keying) are ever swept, and only while
    genuinely idle (no in-flight ``stream()`` call).
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


def max_concurrent_claude_processes() -> int:
    """Process-wide BACKSTOP cap on CONCURRENTLY-CONNECTED ``claude`` CLI subprocesses.

    Resolved via ``providers.claude_code.max_concurrent_processes`` /
    ``CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES`` (file → env → default 4).
    Every pooled entry — the shared base entry AND every scope-keyed entry a
    spawned expert opens — draws from the SAME N slots at connect time and
    releases its slot on disconnect, so the resident CLI-process count this
    process can ever hold is bounded by N regardless of how many concurrent
    scopes exist. A connect beyond the cap WAITS (surfaced, typed, expanding —
    :func:`await_connect_slot` — never fails/degrades) for a slot; this is the
    concurrency lever the idle reap cannot cover, since a genuinely ACTIVE
    fan-out (multiple experts truly streaming at once) is never idle and so is
    never reap-eligible.

    **History (iowarp/clio-agent#1305, 2026-09-03 owner ruling).** #893
    (2026-08-20, commit 0c2a392d) recorded this cap at N=1 against a measured
    memory budget (below). Live evidence proved that N=1 SILENTLY SERIALIZES
    every claude_code LM call process-wide — main and every spawned expert
    draw from the SAME one slot — and once any single call ran long, every
    other queued session starved past both the SDK bridge's 600s per-call
    timeout AND the 900s turn no-progress watchdog, deterministically killing
    otherwise-healthy turns (root-caused live, run-4 traces, #1305). The owner
    ruled 2026-09-03 that clio is **parallel by default**: this cap is a
    resource BACKSTOP (computed runaway protection, like ``MAX_SPAWN_DEPTH`` —
    never a correctness rule) now that #1305 also lands (a) deterministic
    per-subagent connection release (``providers/session_lifecycle.py`` +
    ``ClaudeStreamClientPool.release_session_resources`` — resident CLI count
    tracks actively-streaming agents, not a growing leak) and (b) typed,
    liveness-feeding surfacing for any wait that DOES queue behind the cap
    (:func:`await_connect_slot`) — so a queued connect can no longer silently
    burn either timeout. N=4 was validated live -- see the iowarp/clio-agent#1305
    comment dated 2026-09-03 (deep-researcher run 5: zero stalls at N=4,
    verdict recorded on #1286; traces preserved under ``.grind/traces/``) for
    the evidence; ~300-360 MB RSS per CLI process,
    scripts/mcp_mem_attribution.py. A fresh peak-budget recording against
    ``scripts/mcp_mem_budget.json`` at N=4 is done by the release orchestrator
    on the live box at merge time (not in this worktree — see #1305). Raise
    the default further only alongside ANOTHER fresh, re-verified budget
    recording, never to make a regression pass.

    **The original N=1 measurement (historical, superseded above).** Against
    the recorded 1.42 GB peak budget (5% tolerance), N=4 measured 1.98 GB
    (over) and N=2 measured 1.90 GB on a noisier run (still over) — only N=1
    held reliably under THAT budget. That measurement predates both (a) and
    (b) above: it charged the FULL cost of concurrent connections with none of
    #1305's deterministic release, against a budget that was never re-recorded
    for the parallel-by-default model.
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
# #1305). A queued connect is NOT a failure -- the cap's documented contract is
# "wait, never fail/degrade" -- so this is deliberately a SEPARATE, smaller
# catalog from ``claude_code_sessions.TRANSPORT_FAILURE_REASONS``: same
# discipline (typed, queryable, catalog-driven), different semantics (a
# benign, unbounded wait, not a degraded/dropped connection).
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
    """Typed connect-wait payload (catalog style, mirrors
    :func:`~clio_agent.providers.claude_code_sessions.transport_failure_payload`).
    """
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
# :mod:`clio_agent.arc.rpc_liveness`'s per-attempt backoff shape -- the gap
# between emitted rows GROWS so a long queue-wait never spams the trace, while
# attempt 1 still surfaces promptly (no silent waiting).
_SURFACE_INITIAL_S = 1.0
_SURFACE_MAX_S = 30.0
_SURFACE_BACKOFF_FACTOR = 3.0


async def await_connect_slot(
    slots: "threading.Semaphore",
    *,
    session_id: str = "",
    reclaim_idle_slot: Callable[[], Any] | None = None,
    poll_interval_s: float = 0.2,
) -> None:
    """Wait for a free process-wide connect slot -- surfaced, typed, liveness-fed.

    #1305 root fix: this loop is UNBOUNDED by design (the cap's documented "a
    connect beyond the cap WAITS -- never fails/degrades" contract) and MUST
    run OUTSIDE any per-call SDK timeout region. The caller
    (:meth:`~clio_agent.providers.claude_code_sessions._StreamClientEntry._ensure_client`)
    invokes this BEFORE entering its own timed construct/connect region -- see
    that method's docstring; the per-call timeout then covers only the actual
    SDK exchange, never a queue wait. Two things make a long queue-wait safe
    rather than invisible dead air:

    * **Surfaced** (typed, catalog-driven, :data:`CONNECT_WAIT_REASONS`):
      emitted via ``stream_audit`` at an EXPANDING cadence (mirrors
      :mod:`clio_agent.arc.rpc_liveness`'s per-attempt backoff shape) so a
      short wait logs promptly and a long one never spams the trace.
    * **Counted as turn progress**: every attempt refreshes
      :func:`~clio_agent.runtime.lm_activity.note_lm_activity_for` for
      ``session_id`` -- the SAME per-session bucket the 900s no-progress
      watchdog already reads (:mod:`clio_agent.gact.turn_watchdog`) -- so a
      queued turn is never killed as "no progress"; it is reported as exactly
      what it is, a queue, not a stall.

    Polls the plain ``threading.Semaphore`` with a bounded per-attempt
    ``run_in_executor`` acquire (not one unbounded blocking acquire) so
    cancellation between polls is honored cleanly -- a caller cancelled while
    queued (kill-on-cancel) abandons interest without leaving an orphaned OS
    thread blocked on the semaphore (which would silently consume a LATER
    release as a phantom acquire nothing pairs with). This mirrors the
    pre-#1305 poll shape exactly -- only the side effects between polls
    (surfacing + liveness feed) are new.
    """
    loop = asyncio.get_running_loop()
    start = time.monotonic()
    attempt = 0
    next_surface_at = 0.0  # surface immediately on the first queued attempt
    surface_gap = _SURFACE_INITIAL_S
    while not await loop.run_in_executor(None, slots.acquire, True, poll_interval_s):
        attempt += 1
        if reclaim_idle_slot is not None:
            reclaim_idle_slot()
        elapsed = time.monotonic() - start
        # Progress feed FIRST (cheap, always) -- a queued connect IS turn
        # progress regardless of whether this attempt also gets surfaced.
        from clio_agent.runtime.lm_activity import note_lm_activity_for  # noqa: PLC0415

        note_lm_activity_for(session_id)
        if elapsed >= next_surface_at:
            # Imported from claude_code_sessions (not runtime.stream_audit
            # directly) so a test monkeypatching
            # ``claude_code_sessions.stream_audit`` / ``stream_audit_enabled``
            # (the existing pattern every other audit call site in that
            # module's owner-split siblings already uses) observes this row.
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


def sweep_idle_scoped_entries(
    pool: "ClaudeStreamClientPool", ttl_s: float | None = None
) -> list[tuple[tuple[str, str | None, str | None, str], "_StreamClientEntry"]]:
    """Pop every scope-keyed entry of ``pool`` idle >= ``ttl_s`` (default
    :func:`stream_idle_ttl_s`); the base entry (key[3]=="") is never eligible.

    Returns the evicted ``(key, entry)`` pairs — teardown + the
    stateful-registry notification (:func:`reap_idle_stream_entry`) happen
    OUTSIDE ``pool._guard``: popping first keeps a concurrent ``entry_for``
    for the SAME key from handing out an entry mid-teardown, and neither the
    disconnect scheduling nor the registry lock needs the pool lock held.
    """
    resolved_ttl = stream_idle_ttl_s() if ttl_s is None else ttl_s
    evicted: list[tuple[tuple[str, str | None, str | None, str], Any]] = []
    with pool._guard:  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
        for key, entry in list(pool._entries.items()):  # noqa: SLF001
            if not key[3]:  # base entry — never idle-reaped
                continue
            idle = entry.idle_for()
            if idle is not None and idle >= resolved_ttl:
                evicted.append((key, entry))
        for key, _entry in evicted:
            del pool._entries[key]  # noqa: SLF001
    return evicted


def reap_idle_stream_entry(
    key: tuple[str, str | None, str | None, str], entry: "_StreamClientEntry"
) -> None:
    """Close ``entry`` (idle-reap) and flag its claude_code stateful session so the
    next send on this scope reclassifies as a forced full resend.

    Mirrors the mid-flight
    :meth:`~clio_agent.providers.stateful_common.StatefulSessionRegistry.note_provider_error`
    contract: a scope-keyed connection carries LIVE conversation state in the
    ``claude`` CLI subprocess's own memory, not in ``session_id`` (the
    CONNECTION is the resume boundary — AGENT-COPPER12). Dropping the pooled
    entry without telling the delta registry would let the next send ship a
    delta tail to a fresh subprocess with no memory of the prefix — a silent
    conversation-coherence bug, not merely a reconnect. The registry reset
    makes the drop identical, from the sender's perspective, to any other
    transient transport failure: audited, typed, and healed by a normal full
    send rather than a corrupted delta.
    """
    # Imported from claude_code_sessions (not runtime.stream_audit directly) so a
    # test monkeypatching ``claude_code_sessions.stream_audit`` /
    # ``stream_audit_enabled`` (the existing pattern every other audit call site
    # in that module already uses) observes this emission too.
    from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
        stream_audit,
        stream_audit_enabled,
        transport_failure_payload,
    )
    from clio_agent.providers.claude_code_stateful import stateful_registry  # noqa: PLC0415

    model, cwd, thinking_id, scope = key
    entry.close_nonblocking()
    if stream_audit_enabled():
        stream_audit(
            "provider.transport_error",
            provider="claude_code_sdk",
            transport="sdk",
            model=model,
            **transport_failure_payload("idle_reaped", f"scope={scope!r} idle-reaped"),
        )
    # session_key shape must match resolve_stateful_send's (scope, model, cwd,
    # thinking_key) — pinned by test_claude_code_idle_reap.py so a shape drift
    # in either module fails loudly instead of silently missing the reset.
    stateful_registry().note_provider_error((scope, model, cwd, thinking_id), scope)


# --------------------------------------------------------------------------- #
# #1305 session<->scope ownership bookkeeping (owner-split free functions over
# ClaudeStreamClientPool's ``_session_scopes`` / ``_scope_session`` dicts,
# exactly the ``sweep_idle_scoped_entries`` / ``reap_idle_stream_entry``
# pattern above). This is what lets the GENERIC, GACT-level per-subagent
# release hook (``providers/session_lifecycle.py`` ->
# ``ClaudeStreamClientPool.release_session_resources``) find a session's
# scope-keyed entries WITHOUT knowing the internal, session-UNRELATED
# react-loop scope token ``entry_for`` keys entries on (a fresh uuid per
# ``forward()`` call — see ``stateful_common.stateful_scope``) — it only ever
# has a GACT session id in hand. A SEPARATE registry from
# ``stateful_common``'s scope-registry protocol (``release``/``mark_reset``)
# on purpose: that one is keyed BY the scope token itself (the react loop
# knows its own token); this one is keyed by session id (the completion hook
# does not).
# --------------------------------------------------------------------------- #
def note_scope_owner(
    pool: "ClaudeStreamClientPool", *, scope: str | None, gact_session_id: str
) -> None:
    """Record that ``gact_session_id`` opened ``scope`` (no-op if either is falsy).

    Caller (``entry_for``) already holds ``pool._guard`` -- this does NOT
    acquire it itself (``threading.Lock`` is not reentrant; re-acquiring here
    would deadlock the only real caller).
    """
    if not scope or not gact_session_id:
        return
    pool._session_scopes.setdefault(gact_session_id, set()).add(scope)  # noqa: SLF001
    pool._scope_session[scope] = gact_session_id  # noqa: SLF001


def forget_scope_owner(pool: "ClaudeStreamClientPool", scope: str) -> None:
    """Drop ``scope``'s ownership record (both directions) -- called by every
    release path (:meth:`~claude_code_sessions.ClaudeStreamClientPool.release`)
    so this bookkeeping never outlives the entries it describes. Caller
    already holds ``pool._guard`` -- does NOT acquire it itself (see
    :func:`note_scope_owner`)."""
    owner = pool._scope_session.pop(scope, None)  # noqa: SLF001
    if owner is None:
        return
    owned = pool._session_scopes.get(owner)  # noqa: SLF001
    if owned is not None:
        owned.discard(scope)
        if not owned:
            pool._session_scopes.pop(owner, None)  # noqa: SLF001


def scopes_for_session(pool: "ClaudeStreamClientPool", session_id: str) -> set[str]:
    """Pop + return every scope recorded against ``session_id`` (empty set if none)."""
    if not session_id:
        return set()
    with pool._guard:  # noqa: SLF001 - this module is claude_code_sessions' owner-split sibling
        return pool._session_scopes.pop(session_id, None) or set()  # noqa: SLF001
