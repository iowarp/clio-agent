"""Core of the Claude SDK stateful session-delta transport (#901 / #891).

The TTFT closer holds one detector, one bounded per-loop session registry, and
one per-forward scope for the ``claude_code`` SDK transport. Codex no longer
uses this layer: its official Python SDK is the sole Codex provider boundary.

**The problem (measured, #901/#891).** dspy/litellm is stateless: every LM call
re-sends the FULL rendered prompt and the provider must re-ingest it (cache reads
help but still cost TTFT). Both target providers expose a STATEFUL surface — a
persistent Claude SDK session, or a persistent codex thread — that retains
conversation state server-side; continuing it with only the NEW content is the
provider's native delta.

**Detection is STRUCTURAL, never heuristic.** Delta mode engages only when (a) a
session exists for this (expert-loop) key and (b) the previously-sent message list
is a byte-identical PREFIX of the new list, beneath an optional byte-identical
static trailing block (:func:`classify_delta` / :func:`is_strict_prefix` /
:func:`_delta_beneath_static_tail` — an exact list-prefix check over the message
*dicts*, never fuzzy text). Anything else — the first call, an ARC op that rewrote
the prefix, any mismatch, a bounded LRU eviction, or a mid-delta provider error /
process respawn — is a FULL send under a fresh session handle with a typed reason
(:data:`STATEFUL_RESET_REASONS`, the ``stream_fallback`` house style). No silent
fallback: every reset/degrade is a recorded, queryable reason (#775).

**Session keying is per expert-loop instance, never global.** The registry keys on
``(scope_token, model, cwd, thinking-or-effort)``: parallel experts each hold their
own scope token (a fresh per-``forward`` uuid) so they can never share a session.
The registry is bounded (LRU over live entries) and released explicitly on loop end
(:func:`stateful_scope`'s teardown, which releases EVERY registered provider
registry so every Claude leg tears down from the one scope the ReActV2 loop binds).
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "STATEFUL_RESET_REASONS",
    "DeltaPlan",
    "StatefulSessionRegistry",
    "active_stateful_scope",
    "classify_delta",
    "is_strict_prefix",
    "note_prefix_reset_for_active_scope",
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
            "retried call is a full send on a fresh session handle (bounded by the "
            "LM retry layer)."
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
# Per-forward stateful scope token (the "session token from the V2 loop").
#
# Set ONLY by the ReActV2 loop's ``forward`` (see reactv2._RetainingReActV2), a
# fresh uuid per forward so parallel experts never collide and a new turn always
# starts a fresh session. The classic loop never sets it, so every provider
# transport sees ``None`` and never deltas — the classic wire stays byte-identical.
# --------------------------------------------------------------------------- #
_STATEFUL_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clio_stateful_scope", default=None
)

# Provider registries that must be torn down when a scope ends. Each provider
# registers its process-wide singleton exactly once at module load; test-only
# registries stay unregistered so they never leak across the global scope.
_SCOPE_REGISTRIES: list[StatefulSessionRegistry] = []
_SCOPE_REGISTRIES_LOCK = threading.Lock()


def register_scope_registry(registry: StatefulSessionRegistry) -> None:
    """Register a provider registry for scope-end teardown (idempotent).

    Called once per provider singleton at module load so :func:`stateful_scope`'s
    teardown releases EVERY provider's entries for the ending scope — the ReActV2
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
    (classic loop / feature off), so it is always safe to call.

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

    Entered by the ReActV2 ``forward``. On exit it releases the scope's registry
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


# --------------------------------------------------------------------------- #
# The delta detector — a PURE classification (the unit-proof heart, #901).
# --------------------------------------------------------------------------- #
def is_strict_prefix(prior: list[dict[str, Any]], new: list[dict[str, Any]]) -> bool:
    """Whether ``prior`` is a byte-identical, STRICT prefix of ``new`` (dict compare).

    The structural gate for delta mode: every message dict of ``prior`` must equal
    the message at the same index in ``new`` (structural ``==`` over the dicts — NOT
    fuzzy text, NOT lengths), and ``new`` must be STRICTLY longer so a non-empty
    delta tail exists. An equal list (a resample) is therefore NOT a strict prefix
    (there is no tail to send), and any divergence — including an ARC op that
    rewrote an earlier message — fails here, which is exactly why a rewritten prefix
    can never be sent a delta.

    Args:
        prior: The message list previously sent on the session.
        new: The message list the transport is about to send.

    Returns:
        ``True`` iff a valid non-empty append-only delta ``new[len(prior):]`` exists.
    """
    return len(new) > len(prior) and prior == new[: len(prior)]


def _delta_beneath_static_tail(
    prior: list[dict[str, Any]], new: list[dict[str, Any]], tail_len: int
) -> tuple[int, list[dict[str, Any]]] | None:
    """Append-only delta of ``new`` over ``prior`` beneath a byte-identical static tail.

    The extended structural contract (#901, the residual-tail case). ``tail_len == 0``
    is the pure :func:`is_strict_prefix` case (delta = ``new[len(prior):]``). For
    ``tail_len > 0`` the LAST ``tail_len`` messages of ``prior`` and ``new`` must be
    byte-IDENTICAL (the STATIC tail — dspy's ChatAdapter always appends one such
    trailing message, its ``main_request`` closing instruction, ``adapters/base.py``
    ``format`` l.431-434; its bytes depend only on the output fields, so it never
    changes across a loop, but it necessarily MOVES position as the history grows) and
    the *bodies* (everything before that tail) must satisfy the same strict-prefix
    relation. The delta is then exactly the newly-appended BODY messages — the static
    tail is NOT re-sent (the provider already holds it from the prior send and the loop
    continues the same conversation). This is still a deterministic, typed structural
    comparison — never a fuzzy/heuristic match: it accepts ONLY a byte-identical tail
    plus a byte-identical growing head.

    Returns:
        ``(prefix_len, delta_messages)`` for a valid delta, else ``None``.
    """
    if tail_len == 0:
        if is_strict_prefix(prior, new):
            return len(prior), list(new[len(prior) :])
        return None
    if len(prior) < tail_len or len(new) < tail_len:
        return None
    if prior[len(prior) - tail_len :] != new[len(new) - tail_len :]:
        return None
    prior_body = prior[: len(prior) - tail_len]
    new_body = new[: len(new) - tail_len]
    if len(new_body) > len(prior_body) and prior_body == new_body[: len(prior_body)]:
        return len(prior_body), list(new_body[len(prior_body) :])
    return None


@dataclass(frozen=True)
class DeltaPlan:
    """The pure classification of one call against the session's prior sent-list.

    Attributes:
        mode: ``"delta"`` (send only the appended tail) or ``"full"`` (resend all).
        reason: The typed reset reason (a :data:`STATEFUL_RESET_REASONS` key) for a
            ``full`` send, or ``None`` for a ``delta`` (no reset happened).
        messages: The messages to actually send — the tail for ``delta``, the whole
            list for ``full``.
        prefix_len: The number of reused prefix messages (``delta`` only, else 0).
    """

    mode: Literal["delta", "full"]
    reason: str | None
    messages: list[dict[str, Any]]
    prefix_len: int


def classify_delta(
    prior: list[dict[str, Any]] | None,
    new: list[dict[str, Any]],
    *,
    forced_reason: str | None = None,
    static_tail_len: int = 1,
) -> DeltaPlan:
    """Classify one call into a delta or a typed full-send reset (PURE, no I/O).

    The complete decision matrix, in priority order:

    1. ``forced_reason`` set (an ARC op / eviction / provider error / process respawn
       pre-flagged this key) -> FULL send with that reason. This is the reset ->
       restart mapping: it forces a full send EVEN IF ``new`` would otherwise be a
       valid prefix-extension, so a delta is never sent over a reset prefix.
    2. No ``prior`` (no session yet) -> FULL send, ``first_call``.
    3. ``new`` is an append-only extension of ``prior`` -> DELTA. A two-rung
       STRUCTURAL contract (:func:`_delta_beneath_static_tail`), tried in order:
       (3a) a pure byte-identical strict prefix (:func:`is_strict_prefix`), delta =
       ``new[len(prior):]``; else (3b) an append-only body beneath a byte-identical
       STATIC trailing block of ``static_tail_len`` messages — the residual dspy
       ChatAdapter ``main_request`` closing instruction, which never changes bytes but
       necessarily moves position as the history grows (``adapters/base.py`` l.431-434).
       Both rungs are deterministic and typed; (3b) accepts ONLY a byte-identical tail
       plus a byte-identical growing head, never a fuzzy match.
    4. Otherwise -> FULL send, ``prefix_mismatch``.

    Args:
        prior: The session's previously-sent message list, or ``None`` if none.
        new: The message list about to be sent.
        forced_reason: A pre-flagged typed reset reason that overrides 2–4.
        static_tail_len: The length of the byte-identical static trailing block to
            tolerate at rung 3b (default 1 — the single ChatAdapter closing
            instruction; 0 restricts the contract to a pure strict prefix).

    Returns:
        The :class:`DeltaPlan` describing what to send and why.
    """
    if forced_reason is not None:
        return DeltaPlan("full", forced_reason, list(new), 0)
    if prior is None:
        return DeltaPlan("full", "first_call", list(new), 0)
    for tail_len in (0, static_tail_len) if static_tail_len else (0,):
        found = _delta_beneath_static_tail(prior, new, tail_len)
        if found is not None:
            prefix_len, delta_messages = found
            return DeltaPlan("delta", None, delta_messages, prefix_len)
    return DeltaPlan("full", "prefix_mismatch", list(new), 0)


# --------------------------------------------------------------------------- #
# The bounded, per-loop session registry (provider-agnostic; handle pluggable).
# --------------------------------------------------------------------------- #
@dataclass
class _Entry:
    """One live stateful session and the last full message list it sent."""

    handle: str
    messages: list[dict[str, Any]]
    scope_token: str


class StatefulSessionRegistry:
    """Process-wide, bounded registry of live stateful sessions (per expert-loop key).

    Keyed by ``session_key = (scope_token, model, cwd, thinking-or-effort)`` so
    parallel experts never share a session and a same-loop call reuses its stable
    handle. Bounded by an LRU over live entries (``capacity``); an eviction, an ARC
    op, a provider error, or a process respawn flags the scope so the NEXT call for
    it classifies with the correct typed reason instead of a bare ``first_call``.

    A full send mints a fresh client-side Claude session id. Codex uses its official
    SDK-owned thread lifecycle and does not participate in this registry.
    """

    def __init__(
        self,
        capacity: int | None = None,
        *,
        capacity_resolver: Callable[[], int] | None = None,
    ) -> None:
        self._capacity_resolver = capacity_resolver
        self._capacity = self._resolve_capacity(capacity)
        self._entries: OrderedDict[tuple[Any, ...], _Entry] = OrderedDict()
        # Pending forced reset reason per scope token (ops/eviction/provider error).
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()

    def _resolve_capacity(self, capacity: int | None) -> int:
        """Clamp an explicit capacity, else the resolver's value, else 128."""
        if capacity is not None:
            return max(1, capacity)
        if self._capacity_resolver is not None:
            try:
                return max(1, int(self._capacity_resolver()))
            except Exception:  # noqa: BLE001 - never let config break a turn
                return 128
        return 128

    def plan(
        self,
        *,
        session_key: tuple[Any, ...],
        scope_token: str,
        messages: list[dict[str, Any]],
    ) -> tuple[DeltaPlan, str]:
        """Classify a call and return ``(plan, handle)``, updating live state.

        A pending forced reason for ``scope_token`` (set by :meth:`mark_reset` /
        :meth:`note_provider_error` / an LRU eviction) wins first and is consumed
        here. A ``delta`` reuses the entry's ``handle`` and advances its stored FULL
        list; a ``full`` mints a fresh session id and (re)opens
        the entry, evicting the LRU tail if over capacity.

        Args:
            session_key: The ``(scope_token, model, cwd, thinking-or-effort)`` identity.
            scope_token: The per-forward scope token (for pending-reason lookup).
            messages: The rendered message list about to be sent.
        Returns:
            The :class:`DeltaPlan` and the ``handle`` to send under.
        """
        with self._lock:
            forced = self._pending.pop(scope_token, None)
            entry = self._entries.get(session_key)
            prior = entry.messages if entry is not None else None
            plan = classify_delta(prior, messages, forced_reason=forced)
            if plan.mode == "delta" and entry is not None:
                entry.messages = list(messages)
                self._entries.move_to_end(session_key)
                return plan, entry.handle
            handle = uuid.uuid4().hex
            self._entries[session_key] = _Entry(handle, list(messages), scope_token)
            self._entries.move_to_end(session_key)
            self._evict_over_capacity(protect=session_key)
            return plan, handle

    def _evict_over_capacity(self, *, protect: tuple[Any, ...]) -> None:
        """Drop LRU entries beyond capacity; flag each evicted scope ``session_evicted``.

        Called under ``self._lock``. ``protect`` (the just-stored key) is never the
        eviction victim.
        """
        while len(self._entries) > self._capacity:
            key, entry = next(iter(self._entries.items()))
            if key == protect:  # never evict the entry we just opened
                break
            self._entries.popitem(last=False)
            self._pending[entry.scope_token] = "session_evicted"

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None:
        """Flag ``scope_token`` so its next call is a full send with ``reason``.

        The reset -> restart mapping: an ARC op (compact/delete) that rewrites the
        History prefix, or a codex process respawn, calls this so the next send is a
        typed full send. Validated against :data:`STATEFUL_RESET_REASONS` so a typo
        can never set a silent reason.
        """
        if reason not in STATEFUL_RESET_REASONS:
            raise ValueError(f"Unknown stateful reset reason: {reason}")
        with self._lock:
            self._pending[scope_token] = reason

    def note_provider_error(self, session_key: tuple[Any, ...], scope_token: str) -> None:
        """Drop a poisoned session and flag ``provider_error`` for its next call.

        Called when a send failed mid-flight: the entry is removed (its handle is
        dead) and the scope is flagged so the retried call is a typed
        ``provider_error`` full send on a fresh handle.
        """
        with self._lock:
            self._entries.pop(session_key, None)
            self._pending[scope_token] = "provider_error"

    def release(self, scope_token: str) -> None:
        """Drop every entry + pending flag for ``scope_token`` (loop-end teardown)."""
        with self._lock:
            dead = [key for key, entry in self._entries.items() if entry.scope_token == scope_token]
            for key in dead:
                self._entries.pop(key, None)
            self._pending.pop(scope_token, None)

    def reset_for_tests(self) -> None:
        """Drop all state IN PLACE (test isolation; capacity re-read from config)."""
        with self._lock:
            self._entries.clear()
            self._pending.clear()
            self._capacity = self._resolve_capacity(None)

    @property
    def live_count(self) -> int:
        """Number of live entries (LRU-bound assertions)."""
        with self._lock:
            return len(self._entries)
