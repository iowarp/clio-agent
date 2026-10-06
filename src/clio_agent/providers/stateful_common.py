"""Shared plumbing of the stateful engines (Codex direct sockets, Claude Code sessions).

The conversation logic itself lives in
:class:`clio_agent.lm.engines.conversations.ConversationRegistry` (typed messages, one
kept provider conversation per agent scope). This module holds what every engine and
the agent loop share:

* the typed reset-reason catalog (:data:`STATEFUL_RESET_REASONS`) -- a restart is a
  recorded, queryable reason on the ``provider.stateful`` audit row, never an
  invisible re-key (#775);
* the per-forward stateful scope (:func:`stateful_scope`, bound by
  ``ClioReAct.forward``), its explicit exit for a side call made during a forward
  (:func:`outside_stateful_scope`), and the registry hook that lets an ARC op on a forward reset
  every conversation that forward drove (:func:`note_prefix_reset_for_active_scope`).
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import uuid
from typing import Any, Protocol

__all__ = [
    "STATEFUL_RESET_REASONS",
    "active_stateful_scope",
    "note_prefix_reset_for_active_scope",
    "outside_stateful_scope",
    "register_scope_registry",
    "stateful_reset_payload",
    "stateful_scope",
]

# --------------------------------------------------------------------------- #
# Typed reset-reason catalog (no-silent-fallback, #775 ground rule). Same
# shape/discipline as ``claude_code_sessions.TRANSPORT_FAILURE_REASONS`` and
# ``gact.streaming._stream_fallback_payload``: a session restart is queryable
# structured data recorded per call, never an invisible re-key.
# --------------------------------------------------------------------------- #
STATEFUL_RESET_REASONS: dict[str, dict[str, Any]] = {
    "first_call": {
        "category": "stateful_reset",
        "description": (
            "No stateful session exists yet for this expert-loop key — the first "
            "call of the loop sends its full prompt under a fresh session handle "
            "and opens the session."
        ),
    },
    "prefix_mismatch": {
        "category": "stateful_reset",
        "description": (
            "The new rendered message list is NOT a byte-identical prefix-extension "
            "of the previously-sent list, so no valid delta exists. The session is "
            "restarted with a full send (never a delta over a diverged prefix)."
        ),
    },
    "ops_reset": {
        "category": "stateful_reset",
        "description": (
            "An ARC op (compact/delete) rewrote the History prefix, so the append-"
            "only invariant broke. The op is the one semantically-unavoidable prefix "
            "reset: the session is restarted with a full send."
        ),
    },
    "session_evicted": {
        "category": "stateful_reset",
        "description": (
            "The stored session handle is gone — either the bounded (LRU) registry "
            "evicted this key to stay under capacity, or the pooled provider process "
            "respawned so its server-side thread/session no longer exists. The next "
            "call is a full send that re-opens a fresh session."
        ),
    },
    "provider_error": {
        "category": "stateful_reset",
        "description": (
            "A prior send on this session failed mid-flight (the pooled subprocess "
            "died / the stream broke). The poisoned session is dropped and the "
            "retried call is a full send on a fresh session handle (bounded by "
            "DSPy's retry count)."
        ),
    },
    "provider_compacted": {
        "category": "stateful_reset",
        "description": (
            "The provider compacted its own server-side conversation (e.g. a Codex "
            "``thread/compacted`` notification), so the thread no longer holds what "
            "clio sent. clio-core is the context system, so the thread is abandoned "
            "and the next call is a full send on a fresh session."
        ),
    },
}


def stateful_reset_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured stateful-reset reason payload (catalog style).

    Mirrors :func:`clio_agent.providers.claude_code_sessions.transport_failure_payload`
    and :func:`clio_agent.gact.streaming._stream_fallback_payload`: looks ``reason``
    up in :data:`STATEFUL_RESET_REASONS`, copies its audited metadata, and appends an
    optional free-text ``message``. Raises ``ValueError`` on an unknown reason so a
    typo can never silently produce an empty reason.

    Args:
        reason: A key of :data:`STATEFUL_RESET_REASONS`.
        message: Optional free-text detail appended under ``message``.

    Returns:
        A dict carrying ``reason`` plus the catalog's ``category``/``description``.
    """
    definition = STATEFUL_RESET_REASONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stateful reset reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition}
    if message:
        payload["message"] = message
    return payload


# --------------------------------------------------------------------------- #
# Per-forward stateful scope token.
#
# Set ONLY by ``ClioReAct.forward``: a fresh uuid per forward so parallel experts
# never collide. Outside a loop forward every transport sees ``None`` and sends
# in full.
# --------------------------------------------------------------------------- #
_STATEFUL_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clio_stateful_scope", default=None
)


class ScopeParticipant(Protocol):
    """Anything that takes part in per-forward scope resets + teardown.

    :class:`StatefulSessionRegistry` is one; a provider whose conversations outlive
    a forward registers an adapter instead.
    """

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None: ...

    def release(self, scope_token: str) -> None: ...


# Provider registries that must be torn down when a scope ends. Each provider
# registers its process-wide singleton exactly once at module load; test-only
# registries stay unregistered so they never leak across the global scope.
_SCOPE_REGISTRIES: list[ScopeParticipant] = []
_SCOPE_REGISTRIES_LOCK = threading.Lock()


def register_scope_registry(registry: ScopeParticipant) -> None:
    """Register a provider registry for scope-end teardown (idempotent).

    Called once per provider singleton at module load so :func:`stateful_scope`'s
    teardown releases EVERY provider's entries for the ending scope — the loop
    loop binds one scope and both the claude + codex legs must release from it.
    """
    with _SCOPE_REGISTRIES_LOCK:
        if registry not in _SCOPE_REGISTRIES:
            _SCOPE_REGISTRIES.append(registry)


def active_stateful_scope() -> str | None:
    """The active per-forward stateful scope token, or ``None`` off the V2 loop."""
    return _STATEFUL_SCOPE.get()


def note_prefix_reset_for_active_scope(reason: str = "ops_reset") -> bool:
    """Flag the ACTIVE stateful scope for a typed reset in EVERY registered registry.

    The provider-agnostic ARC-op hook. An ARC prefix-rewrite (compact/delete) breaks
    the append-only invariant for whichever provider leg the active react loop is
    driving, so this marks the active scope in ALL registered provider registries
    (both the ``claude_code`` and ``codex`` legs — mirroring
    :func:`stateful_scope`'s teardown, which releases every registered registry): the
    next send on the active loop is then a typed reset (``reason``, default
    ``ops_reset``) rather than the generic ``prefix_mismatch`` the detector would
    otherwise infer. A no-op returning ``False`` when no stateful scope is active
    (no loop forward / feature off), so it is always safe to call.

    Args:
        reason: A key of :data:`STATEFUL_RESET_REASONS` (default ``"ops_reset"``);
            validated by :meth:`StatefulSessionRegistry.mark_reset` (a typo raises).

    Returns:
        ``True`` if a scope was active and flagged, else ``False``.
    """
    scope = _STATEFUL_SCOPE.get()
    if scope is None:
        return False
    with _SCOPE_REGISTRIES_LOCK:
        registries = list(_SCOPE_REGISTRIES)
    for registry in registries:
        registry.mark_reset(scope, reason)
    return True


@contextlib.contextmanager
def stateful_scope(token: str | None = None) -> Any:
    """Bind a per-forward stateful scope token for the duration of one react loop.

    Entered by ``ClioReAct.forward``. On exit it releases the scope's registry
    entries in EVERY registered provider registry (the #900 explicit-teardown seam —
    a loop's session never outlives the loop). ``token`` defaults to a fresh uuid; an
    explicit token is accepted for tests. Symmetric ``contextvars`` set/reset so
    nested/delegated forwards compose.

    Yields:
        The bound scope token string.
    """
    resolved = token or uuid.uuid4().hex
    var_token = _STATEFUL_SCOPE.set(resolved)
    try:
        yield resolved
    finally:
        _STATEFUL_SCOPE.reset(var_token)
        with _SCOPE_REGISTRIES_LOCK:
            registries = list(_SCOPE_REGISTRIES)
        for registry in registries:
            registry.release(resolved)


@contextlib.contextmanager
def outside_stateful_scope() -> Any:
    """Run a side call made during a forward outside that forward's stateful scope.

    A forward's kept provider conversation is the agent's own ReAct conversation: only
    its steps may continue or replace it. A one-message LM call the forward makes for
    another purpose (DSPy's extract after the final answer, the compaction summary)
    keyed on the same conversation would replace the kept response chain, so the
    agent's next step would be a ``prefix_mismatch`` full send. Inside this block no
    scope is active, so the stateful engines send the call stateless (a one-shot
    conversation of its own) and the forward's kept conversation is left untouched.
    The forward's scope is restored on exit; nothing is released.
    """
    var_token = _STATEFUL_SCOPE.set(None)
    try:
        yield
    finally:
        _STATEFUL_SCOPE.reset(var_token)
