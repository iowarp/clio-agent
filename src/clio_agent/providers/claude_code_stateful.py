"""Stateful-delta SDK transport for ``claude_code`` (#901 / #891, the TTFT closer).

Owner module for the *stateful session-delta* half of the ``claude_code`` SDK
transport — the consumer that dspy ``ReActV2``'s append-only ``dspy.History`` was
adopted to enable. Kept out of the ``claude_code_litellm`` god-file (#775
no-accretion): that file only *calls* :func:`resolve_stateful_send` and hands the
resolved payload / session id to the pooled streaming client
(:mod:`clio_agent.providers.claude_code_sessions`).

**The problem (measured, #901/#891).** tokens/s is at spec but TTFT is ~3.2× the
rated figure even with ~90% provider cache hits, because dspy/litellm is stateless:
every LM call re-sends the FULL rendered prompt (~50–100k tokens) and the provider
must re-ingest it (cache *reads* help but are not free — they still cost TTFT). The
Claude Agent SDK is STATEFUL: a connected ``ClaudeSDKClient`` retains conversation
state in its persistent CLI subprocess (``--input-format stream-json``); continuing
that conversation with another :meth:`ClaudeSDKClient.query` under the SAME
``session_id`` sends ONLY the new content. The delta = the tail of newly-appended
messages.

**Why V2 unblocks it (design ``901_reactv2_design.md`` §3).** Classic dspy ReAct
re-renders the whole trajectory (with fresh field header/footer framing) into ONE
string every step, so no call's wire prompt is a byte-extension of the previous —
the DELTA never held, which is why the earlier prompt-delta layer was measured dead
and stripped (see ``claude_code_sessions`` history). ReActV2 composes each turn as
an append-only ``dspy.History`` of structured messages, so between consecutive
``self.react`` calls the rendered message list differs ONLY by the appended tail.
That makes a TRUE, STRUCTURAL delta possible.

**Detection is STRUCTURAL, never heuristic.** Delta mode engages only when (a) a
session exists for this (expert-loop) key and (b) the previously-sent message list
is a byte-identical PREFIX of the new list (an exact list-prefix check over the
message *dicts*, :func:`is_strict_prefix` — not fuzzy text). Anything else — the
first call, an ARC op that rewrote the prefix, any mismatch, a bounded LRU
eviction, or a mid-delta provider error — is a FULL send under a fresh
``session_id`` with a typed reason (:data:`STATEFUL_RESET_REASONS`, the
``stream_fallback`` house style: ``first_call`` | ``prefix_mismatch`` |
``ops_reset`` | ``session_evicted`` | ``provider_error``). No silent fallback: every
reset/degrade is a recorded, queryable reason (#775).

**Scope / kill-switch.** This whole path is inert unless BOTH the
``stateful_delta`` flag is ON (:func:`stateful_delta_enabled`, default OFF — a new
optimisation rung) AND a per-forward stateful scope token is active
(:func:`active_stateful_scope`, set ONLY by the ReActV2 loop's ``forward``). The
classic ReAct path never sets that token, so it is byte-for-byte unchanged: it
resolves to a full send under a fresh ``session_id`` exactly as before (the
byte-equality suites prove it).

**Session keying is per expert-loop instance, never global.** The registry keys on
``(scope_token, model, cwd, thinking)``: parallel experts each hold their own
scope token (a fresh per-``forward`` uuid) so they can never share a session. The
registry is bounded (LRU over live entries) and released explicitly on loop end
(:func:`stateful_scope`'s teardown, wired from the #900 seams).
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
    "StatefulSend",
    "StatefulSessionRegistry",
    "active_stateful_scope",
    "classify_delta",
    "is_strict_prefix",
    "note_prefix_reset_for_active_scope",
    "resolve_stateful_send",
    "stateful_delta_enabled",
    "stateful_registry",
    "stateful_scope",
    "stateful_reset_payload",
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
            "call of the loop sends its full prompt under a fresh session id and "
            "opens the session."
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
            "The bounded (LRU) stateful-session registry evicted this key's entry to "
            "stay under capacity, so its prior sent-list is gone. The next call is a "
            "full send that re-opens the session."
        ),
    },
    "provider_error": {
        "category": "stateful_reset",
        "description": (
            "A prior send on this session failed mid-flight (the pooled CLI "
            "subprocess died / the stream broke). The poisoned session is dropped "
            "and the retried call is a full send on a fresh session id (bounded by "
            "the LM retry layer)."
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
# Kill-switch + bound config.
# --------------------------------------------------------------------------- #
def stateful_delta_enabled() -> bool:
    """Whether the stateful session-delta transport is enabled (default OFF).

    Resolved via ``providers.claude_code.stateful_delta`` /
    ``CLIO_CLAUDE_CODE_STATEFUL_DELTA`` (file → env → default False). A NEW
    optimisation rung on top of #891's connection pooling: even when ON it only
    engages on the ReActV2 loop (an active :func:`active_stateful_scope`); the
    classic path stays byte-identical full sends.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return bool(
        conf.resolve(
            "providers.claude_code.stateful_delta",
            env="CLIO_CLAUDE_CODE_STATEFUL_DELTA",
            default=False,
            cast=conf.as_bool,
        )
    )


def _registry_capacity() -> int:
    """Max live stateful-session entries before LRU eviction (default 128).

    Override via ``providers.claude_code.stateful_capacity`` /
    ``CLIO_CLAUDE_CODE_STATEFUL_CAPACITY``. Clamped to ``>= 1`` so the registry can
    always hold at least the in-flight expert.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        n = int(
            conf.resolve(
                "providers.claude_code.stateful_capacity",
                env="CLIO_CLAUDE_CODE_STATEFUL_CAPACITY",
                default=128.0,
                cast=conf.as_float,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break a turn
        return 128
    return max(1, n)


# --------------------------------------------------------------------------- #
# Per-forward stateful scope token (the "session token from the V2 loop").
#
# Set ONLY by the ReActV2 loop's ``forward`` (see reactv2._RetainingReActV2), a
# fresh uuid per forward so parallel experts never collide and a new turn always
# starts a fresh session. The classic loop never sets it, so the transport sees
# ``None`` and never deltas — the classic wire stays byte-identical.
# --------------------------------------------------------------------------- #
_STATEFUL_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clio_claude_code_stateful_scope", default=None
)


def active_stateful_scope() -> str | None:
    """The active per-forward stateful scope token, or ``None`` off the V2 loop."""
    return _STATEFUL_SCOPE.get()


@contextlib.contextmanager
def stateful_scope(token: str | None = None) -> Any:
    """Bind a per-forward stateful scope token for the duration of one react loop.

    Entered by the ReActV2 ``forward``. On exit it releases the scope's registry
    entries (the #900 explicit-teardown seam — a loop's session never outlives the
    loop). ``token`` defaults to a fresh uuid; an explicit token is accepted for
    tests. Symmetric ``contextvars`` set/reset so nested/delegated forwards compose.

    Yields:
        The bound scope token string.
    """
    resolved = token or uuid.uuid4().hex
    var_token = _STATEFUL_SCOPE.set(resolved)
    try:
        yield resolved
    finally:
        _STATEFUL_SCOPE.reset(var_token)
        stateful_registry().release(resolved)


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

    1. ``forced_reason`` set (an ARC op / eviction / provider error pre-flagged this
       key) -> FULL send with that reason. This is the ops-reset -> restart mapping:
       it forces a full send EVEN IF ``new`` would otherwise be a valid prefix-
       extension, so a delta is never sent over a reset prefix.
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
# The bounded, per-loop session registry.
# --------------------------------------------------------------------------- #
@dataclass
class _Entry:
    """One live stateful session: its stable id + the last FULL list it sent."""

    session_id: str
    messages: list[dict[str, Any]]
    scope_token: str


class StatefulSessionRegistry:
    """Process-wide, bounded registry of live stateful sessions (per expert-loop key).

    Keyed by ``session_key = (scope_token, model, cwd, thinking)`` so parallel
    experts never share a session and a same-loop call reuses its stable
    ``session_id``. Bounded by an LRU over live entries (:func:`_registry_capacity`);
    an eviction, an ARC op, or a provider error flags the scope so the NEXT call for
    it classifies with the correct typed reason instead of a bare ``first_call``.
    """

    def __init__(self, capacity: int | None = None) -> None:
        self._capacity = capacity if capacity is not None else _registry_capacity()
        self._entries: OrderedDict[tuple[Any, ...], _Entry] = OrderedDict()
        # Pending forced reset reason per scope token (ops/eviction/provider error).
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()

    def plan(
        self,
        *,
        session_key: tuple[Any, ...],
        scope_token: str,
        messages: list[dict[str, Any]],
    ) -> tuple[DeltaPlan, str]:
        """Classify a call and return ``(plan, session_id)``, updating live state.

        A pending forced reason for ``scope_token`` (set by :meth:`mark_reset` /
        :meth:`note_provider_error` / an LRU eviction) wins first and is consumed
        here. A ``delta`` reuses the entry's ``session_id`` and advances its stored
        FULL list; a ``full`` mints a fresh ``session_id`` and (re)opens the entry,
        evicting the LRU tail if over capacity.

        Args:
            session_key: The ``(scope_token, model, cwd, thinking)`` identity.
            scope_token: The per-forward scope token (for pending-reason lookup).
            messages: The rendered message list about to be sent.

        Returns:
            The :class:`DeltaPlan` and the ``session_id`` to send under.
        """
        with self._lock:
            forced = self._pending.pop(scope_token, None)
            entry = self._entries.get(session_key)
            prior = entry.messages if entry is not None else None
            plan = classify_delta(prior, messages, forced_reason=forced)
            if plan.mode == "delta" and entry is not None:
                entry.messages = list(messages)
                self._entries.move_to_end(session_key)
                return plan, entry.session_id
            session_id = uuid.uuid4().hex
            self._entries[session_key] = _Entry(session_id, list(messages), scope_token)
            self._entries.move_to_end(session_key)
            self._evict_over_capacity(protect=session_key)
            return plan, session_id

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

        The ops-reset -> restart mapping: an ARC op (compact/delete) that rewrites
        the History prefix calls this so the next send is a typed ``ops_reset`` full
        send. Validated against :data:`STATEFUL_RESET_REASONS` so a typo can never
        set a silent reason.
        """
        if reason not in STATEFUL_RESET_REASONS:
            raise ValueError(f"Unknown stateful reset reason: {reason}")
        with self._lock:
            self._pending[scope_token] = reason

    def note_provider_error(self, session_key: tuple[Any, ...], scope_token: str) -> None:
        """Drop a poisoned session and flag ``provider_error`` for its next call.

        Called when a send failed mid-flight: the entry is removed (its ``session_id``
        is dead) and the scope is flagged so the retried call is a typed
        ``provider_error`` full send on a fresh id.
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
            self._capacity = _registry_capacity()

    @property
    def live_count(self) -> int:
        """Number of live entries (LRU-bound assertions)."""
        with self._lock:
            return len(self._entries)


_REGISTRY = StatefulSessionRegistry()


def stateful_registry() -> StatefulSessionRegistry:
    """Return the process-wide stateful-session registry singleton."""
    return _REGISTRY


def note_prefix_reset_for_active_scope(reason: str = "ops_reset") -> bool:
    """Flag the ACTIVE stateful scope for a typed reset (the ARC-op hook).

    The seam an ARC prefix-rewrite (compact/delete) calls so the next send on the
    active react loop is a typed reset rather than a bare ``prefix_mismatch``. A
    no-op returning ``False`` when no stateful scope is active (classic loop /
    feature off), so it is always safe to call.

    Returns:
        ``True`` if a scope was flagged, else ``False``.
    """
    scope = active_stateful_scope()
    if scope is None:
        return False
    _REGISTRY.mark_reset(scope, reason)
    return True


# --------------------------------------------------------------------------- #
# The transport entry point + its instrumentation.
# --------------------------------------------------------------------------- #
@dataclass
class StatefulSend:
    """A resolved send plan the transport executes (payload + session id + audit).

    Attributes:
        payload: The serialized string to ``query`` — the delta tail or the full
            prompt.
        session_id: The ``session_id`` to send under (stable across a delta run,
            fresh on any reset).
        mode: ``"delta"`` or ``"full"``.
        reason: The typed reset reason for a ``full`` send when the session was
            engaged, else ``None``.
        delta_chars: Characters actually sent this call (``len(payload)``) — what the
            waterfall attributes TTFT against.
        engaged: Whether the stateful path was active (flag ON + a scope token). When
            ``False`` the transport behaves byte-for-byte like the pre-#901 path.
        session_key: The registry key (for :meth:`note_error`), or ``None`` when not
            engaged.
        scope_token: The active scope token, or ``None`` when not engaged.
        call_id: The per-LM-call correlation id the transport reuses for
            ``emit_call_started`` so the ``provider.stateful`` audit row and the
            ``provider.call_started`` / ``raw_event`` TTFT markers join on ONE id (the
            waterfall attributes TTFT by ``stateful_mode``). Minted here so the mode
            classification and the call marker cannot drift onto separate ids.
    """

    payload: str
    session_id: str
    mode: str
    reason: str | None
    delta_chars: int
    engaged: bool
    session_key: tuple[Any, ...] | None = None
    scope_token: str | None = None
    call_id: str = ""

    def note_error(self) -> None:
        """Drop the poisoned session on a mid-flight send failure (no-op if not engaged)."""
        if self.engaged and self.session_key is not None and self.scope_token is not None:
            _REGISTRY.note_provider_error(self.session_key, self.scope_token)


def resolve_stateful_send(
    *,
    messages: list[dict[str, Any]],
    full_prompt: str,
    model: str,
    cwd: str | None,
    thinking: dict[str, Any] | None,
    serialize: Callable[[list[dict[str, Any]]], str],
    call_index: int = 0,
) -> StatefulSend:
    """Resolve one SDK call into a full-or-delta send plan (the transport seam).

    When the stateful path is inert (flag OFF or no active scope token) this returns
    a plain full send under a FRESH ``session_id`` — byte-for-byte the pre-#901
    transport, no audit row, no registry touch. When engaged it asks the registry
    for a :class:`DeltaPlan`, serializes the delta tail with the caller's own
    ``serialize`` (so the delta bytes match the full path's serialization exactly),
    and records a typed ``provider.stateful`` audit row (``stateful_mode`` +
    ``delta_chars`` + reason) so the orchestrator's waterfall can attribute TTFT by
    mode.

    Args:
        messages: The rendered chat-message dicts (the prefix-check operand).
        full_prompt: The already-serialized full prompt (sent on any full/reset).
        model: The clean model id (part of the session key).
        cwd: The transport cwd (part of the session key).
        thinking: The resolved SDK thinking config (part of the session key).
        serialize: The transport's message-list serializer (used for the delta tail).
        call_index: The provider call index, for the audit row.

    Returns:
        The :class:`StatefulSend` the transport executes.
    """
    # One correlation id per LM call, minted here and reused by the transport's
    # ``emit_call_started`` so the ``provider.stateful`` row (which carries the mode)
    # and the ``provider.call_started`` / ``raw_event`` TTFT markers join on ONE id.
    call_id = uuid.uuid4().hex
    scope = active_stateful_scope()
    if scope is None or not stateful_delta_enabled():
        # Inert: byte-identical pre-#901 behaviour (fresh id, full prompt).
        return StatefulSend(
            payload=full_prompt,
            session_id=uuid.uuid4().hex,
            mode="full",
            reason=None,
            delta_chars=len(full_prompt),
            engaged=False,
            call_id=call_id,
        )

    from clio_agent.providers.claude_code_options import thinking_key  # noqa: PLC0415

    session_key = (scope, model, cwd, thinking_key(thinking))
    plan, session_id = _REGISTRY.plan(
        session_key=session_key, scope_token=scope, messages=messages
    )
    payload = serialize(plan.messages) if plan.mode == "delta" else full_prompt
    send = StatefulSend(
        payload=payload,
        session_id=session_id,
        mode=plan.mode,
        reason=plan.reason,
        delta_chars=len(payload),
        engaged=True,
        session_key=session_key,
        scope_token=scope,
        call_id=call_id,
    )
    _audit_stateful(send, model=model, call_index=call_index, prefix_len=plan.prefix_len, total=len(messages))
    return send


def _audit_stateful(
    send: StatefulSend, *, model: str, call_index: int, prefix_len: int, total: int
) -> None:
    """Emit one ``provider.stateful`` audit row (no-silent-fallback house style).

    A no-op unless ``CLIO_STREAM_AUDIT_LOG`` is configured. Carries ``stateful_mode``
    (delta|full), ``delta_chars`` (chars actually sent), the typed reset ``reason``
    (via :func:`stateful_reset_payload` when a reset happened), the reused
    ``prefix_len`` and the ``total`` message count — the fields the orchestrator's
    waterfall needs to attribute TTFT by mode.
    """
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "claude_code_sdk",
        "transport": "sdk",
        "model": f"claude_code/{model}",
        "call_id": send.call_id,
        "call_index": call_index,
        "stateful_mode": send.mode,
        "delta_chars": send.delta_chars,
        "prefix_messages": prefix_len,
        "total_messages": total,
        "session_id": send.session_id,
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
