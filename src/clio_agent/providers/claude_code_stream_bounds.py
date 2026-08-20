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
  :class:`ClaudeStreamClientPool`'s connect gate) — the idle reap cannot
  bound a genuinely ACTIVE fan-out (multiple experts truly streaming at
  once, e.g. ``spawn_agents_parallel``): those connections are busy, not
  idle, by design. The cap makes an over-the-limit connect WAIT for a free
  slot rather than fail or degrade, bounding total resident subprocesses
  regardless of how many concurrent scopes exist.

Both levers exist because the standard acceptance load's memory budget
(``scripts/mcp_mem_budget.json``) was recorded before e47285fb (#COPPER12,
"scope-keyed stream connections") — a spawned child's own isolated connection
is the CORRECT fix for a real cross-conversation bleed, but it also means
resource cost now scales with concurrently-active scopes rather than staying
pinned at one connection process-wide. Reaping the idle case and bounding
the active-concurrency case are how that correctness fix stays inside the
recorded budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.providers.claude_code_sessions import (
        ClaudeStreamClientPool,
        _StreamClientEntry,
    )

__all__ = [
    "max_concurrent_claude_processes",
    "reap_idle_stream_entry",
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
    """Process-wide cap on CONCURRENTLY-CONNECTED ``claude`` CLI subprocesses.

    Resolved via ``providers.claude_code.max_concurrent_processes`` /
    ``CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES`` (file → env → default 1).
    Every pooled entry — the shared base entry AND every scope-keyed entry a
    spawned expert opens — draws from the SAME N slots at connect time and
    releases its slot on disconnect, so the resident CLI-process count this
    process can ever hold is bounded by N regardless of how many concurrent
    scopes exist. A connect beyond the cap WAITS (never fails/degrades) for a
    slot — an expert whose turn is slow to start is still correct, just
    queued; this is the concurrency lever the idle reap cannot cover, since a
    genuinely ACTIVE fan-out (multiple experts truly streaming at once) is
    never idle and so is never reap-eligible.

    The default is set by the measured per-process cost against the RECORDED
    budget, not a round number: a live claude-sdk-cli process runs ~300-360 MB
    RSS (measured live, scripts/mcp_mem_attribution.py), and the standard
    acceptance load's own non-claude floor already varies run to run (~0.45-
    0.55 GB for server-main + the external clio-core daemon, before counting
    any MCP-fleet or host noise the same live run picks up). Against the
    recorded 1.42 GB peak budget (scripts/mcp_mem_budget.json, 5% tolerance),
    N=4 measured 1.98 GB (over), N=2 measured 1.90 GB on a noisier run (still
    over) — only N=1 held reliably under budget across repeated live runs.
    Serializing every claude_code CLI call process-wide is a real latency
    cost or a genuinely concurrent multi-expert turn (paid once, not
    correctness-affecting — a queued connect always completes, never fails),
    but it is what the CURRENT recorded budget requires; raise it only
    alongside a fresh, lower budget recording that has re-verified headroom,
    never to make a regression pass.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return max(
        1,
        int(
            conf.resolve(
                "providers.claude_code.max_concurrent_processes",
                env="CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES",
                default=1.0,
                cast=conf.as_float,
            )
        ),
    )


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
