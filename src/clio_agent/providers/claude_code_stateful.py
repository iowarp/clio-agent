"""Stateful-delta SDK transport for ``claude_code`` (#901 / #891, the TTFT closer).

Owner module for the *stateful session-delta* half of the ``claude_code`` SDK
transport — the consumer that dspy ``ReActV2``'s append-only ``dspy.History`` was
adopted to enable. Kept out of the ``claude_code_litellm`` god-file (#775
no-accretion): that file only *calls* :func:`resolve_stateful_send` and hands the
resolved payload / session id to the pooled streaming client
(:mod:`clio_agent.providers.claude_code_sessions`).

**The provider-agnostic core moved out (#891 codex slice).** The delta detector,
the typed reset catalog, the per-forward scope, and the bounded session registry
are now shared with the native ``codex app-server`` transport
(:mod:`clio_agent.providers.codex_stateful`) in
:mod:`clio_agent.providers.stateful_common` — the prefix-classification and
registry lifecycle are identical across providers; only the *send side* differs. To
preserve every importer / test, this module RE-EXPORTS those names unchanged
(:data:`STATEFUL_RESET_REASONS`, :func:`classify_delta`, :func:`is_strict_prefix`,
:class:`StatefulSessionRegistry`, :func:`stateful_scope`, ...). What stays here is
purely the ``claude_code`` send side: the SDK ``query`` under a stable
``session_id`` and the ``claude_code``-specific flag / capacity config / audit row.

**The problem (measured, #901/#891).** dspy/litellm is stateless: every LM call
re-sends the FULL rendered prompt (~50–100k tokens) and the provider must re-ingest
it (cache reads help but still cost TTFT). The Claude Agent SDK is STATEFUL: a
connected ``ClaudeSDKClient`` retains conversation state in its persistent CLI
subprocess (``--input-format stream-json``); continuing that conversation with
another :meth:`ClaudeSDKClient.query` under the SAME ``session_id`` sends ONLY the
new content. The delta = the tail of newly-appended messages.

**Scope / kill-switch.** This whole path is inert unless BOTH the ``stateful_delta``
flag is ON (:func:`stateful_delta_enabled`, default ON) AND a per-forward stateful
scope token is active (:func:`active_stateful_scope`, set ONLY by the ReActV2 loop's
``forward``). The classic ReAct path never sets that token, so it is byte-for-byte
unchanged: it resolves to a full send under a fresh ``session_id`` exactly as before
(the byte-equality suites prove it).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Re-export the provider-agnostic core so every existing importer / test that reads
# these off ``claude_code_stateful`` keeps working unchanged.
from clio_agent.providers.stateful_common import (
    STATEFUL_RESET_REASONS,
    DeltaPlan,
    StatefulSessionRegistry,
    _delta_beneath_static_tail,
    active_stateful_scope,
    classify_delta,
    is_strict_prefix,
    note_prefix_reset_for_active_scope,
    register_scope_registry,
    stateful_reset_payload,
    stateful_scope,
)

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

# ``_delta_beneath_static_tail`` is a PRIVATE helper re-exported only so the
# detector unit tests can pin it directly off this module; naming it here marks the
# deliberate re-export (the public names are covered by ``__all__``).
_DELTA_BENEATH_STATIC_TAIL = _delta_beneath_static_tail


# --------------------------------------------------------------------------- #
# Kill-switch + bound config (``claude_code``-specific).
# --------------------------------------------------------------------------- #
def stateful_delta_enabled() -> bool:
    """Whether the stateful session-delta transport is enabled (default ON).

    Resolved via ``providers.claude_code.stateful_delta`` /
    ``CLIO_CLAUDE_CODE_STATEFUL_DELTA`` (file → env → default True; set ``=0``
    to opt out). Default flipped ON on live acceptance evidence (#893): delta
    TTFT 1.79s vs 7+s cold, 76.7% cached-input, typed reset catalog covering
    every fallback-to-full-send path. Even when ON it only engages on the
    ReActV2 loop (an active :func:`active_stateful_scope`); the classic path
    stays byte-identical full sends.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return bool(
        conf.resolve(
            "providers.claude_code.stateful_delta",
            env="CLIO_CLAUDE_CODE_STATEFUL_DELTA",
            default=True,
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


# The process-wide ``claude_code`` registry singleton, registered for scope-end
# teardown so the ReActV2 loop's one scope releases this leg too.
_REGISTRY = StatefulSessionRegistry(capacity_resolver=_registry_capacity)
register_scope_registry(_REGISTRY)


def stateful_registry() -> StatefulSessionRegistry:
    """Return the process-wide ``claude_code`` stateful-session registry singleton."""
    return _REGISTRY


# ``note_prefix_reset_for_active_scope`` is imported from ``stateful_common`` and
# re-exported here (see the import block + ``__all__``): the ARC-op reset hook is
# provider-agnostic — a compact/delete rewrites the prefix for whichever provider
# leg the active loop drives, so it must flag the active scope in BOTH the
# ``claude_code`` and ``codex`` registries, not just this module's ``_REGISTRY``.


# --------------------------------------------------------------------------- #
# The transport entry point + its instrumentation (``claude_code`` send side).
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
            ``provider.call_started`` / ``raw_event`` TTFT markers join on ONE id.
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
    _audit_stateful(
        send, model=model, call_index=call_index, prefix_len=plan.prefix_len, total=len(messages)
    )
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
