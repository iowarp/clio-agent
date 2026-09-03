"""Generic, provider-agnostic per-subagent connection-lifecycle release (#1305).

Owner ruling (2026-09-03, iowarp/clio-agent#1305): **connection lifetime =
subagent lifetime.** History: pre-#893 was "every message gets its own SDK
client" (memory blowup); #893 over-corrected to "one global slot" (a
process-wide connect gate that deterministically starved queued sessions
under a long call — proven live, run-4 traces). The correct model is neither
extreme: every subagent keeps its OWN provider connection for its whole life
(concurrency = live agents, parallel by default — the process-wide
concurrency cap in :mod:`clio_agent.providers.claude_code_stream_bounds`
survives only as a resource BACKSTOP, a computed runaway guard like
``MAX_SPAWN_DEPTH``, never a correctness rule), and that connection dies
DETERMINISTICALLY the instant the subagent's work is done — never guessed at
by an idle-TTL sweep (the TTL stays as a safety net for paths this hook
misses, not the primary release). Resurrection is automatic: a released
provider simply reconnects on its next use (the existing connect-on-demand
path every provider already has); this module supplies the missing half,
deterministic release.

**The seam is generic, not a claude_code special.** Any provider that holds
per-session resources (a persistent SDK client, a pooled subprocess, a
long-lived thread) registers a ``release_session_resources(session_id)``
callback here once at import time (mirrors
:func:`clio_agent.providers.stateful_common.register_scope_registry`'s
established pattern — a SEPARATE registry because that one is keyed by an
internal, session-UNRELATED react-loop scope token, minted fresh per
``forward()`` call, while THIS one is keyed by the GACT session id the
completion hook actually has in hand). :func:`release_session_resources` is
the single dispatch point, called from the ONE choke point every child
agent-task completion path funnels through —
``gact/task_fold.py::finish_agent_task_transition``, already race-guarded
exactly-once by that fold's own ``applied``/``is_terminal`` winner check (see
that module for why it — not ``agent_tasks.py``'s raw registry transition,
which fires for every racing caller including the loser — is the right hook).

Consumer #1 (this slice): claude_code's ``ClaudeStreamClientPool`` (see
:meth:`clio_agent.providers.claude_code_sessions.ClaudeStreamClientPool.release_session_resources`).
Consumer #2 (NOT implemented this slice — the seam must not preclude it): the
codex CLI provider's persistent thread/session, today its own SDK-owned
lifecycle with zero participation in any shared registry; wiring it in later
is a follow-on, not a claude-only special case (#775 no privileged
integrations).

Dispatch is best-effort per provider: one provider's release failure is
logged with a typed, queryable reason (#775 no-silent-fallback — never a bare
``except: pass``) and must never stop another provider's release, and must
never propagate into the agent-task completion path that triggered it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "SESSION_RELEASE_FAILED_REASON",
    "register_session_lifecycle_provider",
    "release_session_resources",
    "reset_for_tests",
]

#: Typed reason (#775 catalog discipline) for a provider's release callback
#: raising. Not a full reason-catalog dict (this module has exactly one
#: failure mode to name) — just a stable, queryable string other typed-reason
#: catalogs in this codebase (``TRANSPORT_FAILURE_REASONS``,
#: ``STATEFUL_RESET_REASONS``) would key on if this ever grows one.
SESSION_RELEASE_FAILED_REASON = "session_release_failed"

_PROVIDERS: list[Callable[[str], None]] = []
_GUARD = threading.Lock()


def register_session_lifecycle_provider(release: Callable[[str], None]) -> None:
    """Register a provider's ``release_session_resources(session_id)`` callback.

    Idempotent by identity (mirrors
    :func:`clio_agent.providers.stateful_common.register_scope_registry`):
    call once per provider singleton at module load so
    :func:`release_session_resources` dispatches to every registered leg.
    """
    with _GUARD:
        if release not in _PROVIDERS:
            _PROVIDERS.append(release)


def release_session_resources(session_id: str) -> None:
    """Dispatch one subagent's completion to every registered provider (#1305).

    Called from the single choke point every child agent-task completion path
    funnels through (``gact/task_fold.py::finish_agent_task_transition``) the
    moment that subagent's ``AgentTask`` reaches a terminal status. A no-op
    for an empty/missing ``session_id`` (defensive — this must never be the
    reason a completion path breaks).

    Best-effort per provider: a release failure is logged (typed,
    :data:`SESSION_RELEASE_FAILED_REASON`) and does NOT stop the remaining
    providers from running, and never propagates into the caller — the
    agent-task completion path that triggered this must never fail or stall
    because a provider's own cleanup misbehaved.
    """
    if not session_id:
        return
    with _GUARD:
        providers = list(_PROVIDERS)
    for release in providers:
        try:
            release(session_id)
        except Exception:  # noqa: BLE001 - one provider's failure must never break another's
            logger.warning(
                "session lifecycle release failed: reason=%s session_id=%s provider=%s",
                SESSION_RELEASE_FAILED_REASON,
                session_id,
                getattr(release, "__qualname__", repr(release)),
                exc_info=True,
            )


def reset_for_tests() -> None:
    """Drop all registered providers IN PLACE (test isolation)."""
    with _GUARD:
        _PROVIDERS.clear()
