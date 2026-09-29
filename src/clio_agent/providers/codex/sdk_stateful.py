"""Stateful thread transport for the Codex SDK provider (one thread per conversation).

The Codex SDK is a STATEFUL provider: an ``AsyncThread`` holds the conversation and
runs one or more turns, so continuing it with only the NEW messages is the provider's
native delta. Before this module every LM call opened a fresh ephemeral thread and
re-sent the whole rendered history (the 39-minute OPAL turn: 79 steps, 9.87M input
tokens). This module decides, per call, whether to continue an existing thread with a
delta or open a new one with a full send.

**Detection is the shared structural rule.** The delta plan comes from
:class:`~clio_agent.providers.stateful_common.StatefulSessionRegistry` /
:func:`~clio_agent.providers.stateful_common.classify_delta` — the same detector the
Claude Code transport uses: a byte-identical message-list prefix beneath one static
trailing message (DSPy's "Respond with …" block, which DSPy re-renders at the tail on
every call and is never removed). Anything else is a full send on a new thread with a
typed :data:`~clio_agent.providers.stateful_common.STATEFUL_RESET_REASONS` reason.

**A conversation spans turns.** The registry key is the GACT session plus the agent's
(run-keyed) scope, the model, cwd and reasoning effort — NOT the per-forward scope
token the Claude leg uses — so a thread survives across the user's turns. Until the
agent-context projection is append-only across turns, the first call of a new turn
classifies ``prefix_mismatch`` (typed) and opens a new thread; within a turn every
call after the first is a delta.

**The per-forward scope still gates it.** The path engages only inside a ReAct loop
(an active :func:`~clio_agent.providers.stateful_common.active_stateful_scope`) with a
GACT session and a react scope. Anywhere else (the compaction summarizer, the goal
judge, CLI calls) the call is a plain full send on an ephemeral thread, exactly as
before. An ARC op that rewrites the prefix calls
:func:`~clio_agent.providers.stateful_common.note_prefix_reset_for_active_scope`; the
scope adapter below maps that per-forward token to the conversations it drove.

**Threads are released.** A superseded or evicted thread is archived on the SDK
(:meth:`CodexSDKClient.archive_thread`); a failure to archive is logged with a typed
reason, never swallowed.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clio_agent.providers.stateful_common import (
    StatefulSessionRegistry,
    active_stateful_scope,
    register_scope_registry,
    stateful_reset_payload,
)

__all__ = [
    "CodexSend",
    "codex_stateful_registry",
    "resolve_codex_send",
]


def _registry_capacity() -> int:
    """Max live Codex conversations before LRU eviction (default 128).

    Override via ``providers.codex.stateful_capacity`` /
    ``CLIO_CODEX_STATEFUL_CAPACITY``. Clamped to ``>= 1``.
    """
    from clio_agent import conf  # noqa: PLC0415

    n = int(
        conf.resolve(
            "providers.codex.stateful_capacity",
            env="CLIO_CODEX_STATEFUL_CAPACITY",
            default=128.0,
            cast=conf.as_float,
        )
    )
    return max(1, n)


_REGISTRY = StatefulSessionRegistry(capacity_resolver=_registry_capacity)


def codex_stateful_registry() -> StatefulSessionRegistry:
    """Return the process-wide Codex conversation registry."""
    return _REGISTRY


# --------------------------------------------------------------------------- #
# Slot -> thread binding. The registry mints a client-side slot id on every full
# send; the Codex thread id is server-minted when the thread starts, so the
# transport binds slot -> thread here once the SDK reports it.
# --------------------------------------------------------------------------- #
_BINDINGS_LOCK = threading.Lock()
# conversation token -> (slot, thread_id); bounded like the registry.
_BINDINGS: OrderedDict[str, tuple[str, str]] = OrderedDict()
# per-forward scope token -> conversation tokens it drove (for ARC-op resets).
_FORWARD_CONVERSATIONS: dict[str, set[str]] = {}
# Threads to archive, drained by the transport (archival needs the SDK loop).
_TO_ARCHIVE: list[str] = []


def _conversation_token() -> str | None:
    """``"<gact session>::<run-keyed react scope>"`` for the active loop, else ``None``."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    session_id = (_ctx.active_session_id() or "").strip()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    if not session_id or not scope:
        return None
    return f"{session_id}::{scope}"


def _bind(conversation: str, slot: str, thread_id: str) -> None:
    """Record ``slot -> thread_id`` for ``conversation``; queue a superseded thread."""
    with _BINDINGS_LOCK:
        previous = _BINDINGS.pop(conversation, None)
        if previous is not None and previous[1] and previous[1] != thread_id:
            _TO_ARCHIVE.append(previous[1])
        _BINDINGS[conversation] = (slot, thread_id)
        capacity = _registry_capacity()
        while len(_BINDINGS) > capacity:
            _evicted, (_slot, old_thread) = _BINDINGS.popitem(last=False)
            if old_thread:
                _TO_ARCHIVE.append(old_thread)


def _bound_thread(conversation: str, slot: str) -> str | None:
    """The thread bound to ``conversation`` under ``slot``, or ``None``."""
    with _BINDINGS_LOCK:
        entry = _BINDINGS.get(conversation)
        if entry is None or entry[0] != slot:
            return None
        _BINDINGS.move_to_end(conversation)
        return entry[1]


def _drop_binding(conversation: str) -> None:
    """Forget ``conversation``'s thread and queue it for archival."""
    with _BINDINGS_LOCK:
        entry = _BINDINGS.pop(conversation, None)
        if entry is not None and entry[1]:
            _TO_ARCHIVE.append(entry[1])


def take_threads_to_archive() -> list[str]:
    """Pop every thread id queued for archival (drained by the transport)."""
    with _BINDINGS_LOCK:
        pending = list(_TO_ARCHIVE)
        _TO_ARCHIVE.clear()
        return pending


class _ForwardScopeAdapter:
    """Maps the per-forward scope protocol onto conversation-keyed Codex state.

    ``stateful_common`` flags / releases registries by the PER-FORWARD scope token.
    Codex conversations outlive a forward, so:

    * ``mark_reset(token, reason)`` flags every conversation this forward drove, so
      the next call on each is a typed full send (an ARC op rewrote the prefix);
    * ``release(token)`` forgets only the forward -> conversation mapping — the
      threads themselves survive into the next turn.
    """

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None:
        with _BINDINGS_LOCK:
            conversations = set(_FORWARD_CONVERSATIONS.get(scope_token, set()))
        for conversation in conversations:
            _REGISTRY.mark_reset(conversation, reason)

    def release(self, scope_token: str) -> None:
        with _BINDINGS_LOCK:
            _FORWARD_CONVERSATIONS.pop(scope_token, None)


register_scope_registry(_ForwardScopeAdapter())


@dataclass
class CodexSend:
    """A resolved Codex SDK send plan.

    Attributes:
        messages: The message slice to serialize and send (delta tail or all).
        mode: ``"delta"`` (continue ``thread_id``) or ``"full"`` (new thread).
        reason: Typed reset reason for an engaged full send, else ``None``.
        engaged: Whether the stateful path is active for this call.
        thread_id: The thread to continue on a delta; ``None`` on a full send.
        conversation: The conversation token (engaged only).
        slot: The registry slot for this send (engaged only).
        session_key: The registry key (engaged only).
        call_id: Correlation id reused by the SDK audit rows.
    """

    messages: list[dict[str, Any]]
    mode: str
    reason: str | None
    engaged: bool
    thread_id: str | None = None
    conversation: str | None = None
    slot: str | None = None
    session_key: tuple[Any, ...] | None = None
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def bind_thread(self, thread_id: str) -> None:
        """Record the thread a full send opened so the next call can continue it."""
        if self.engaged and self.conversation is not None and self.slot is not None:
            _bind(self.conversation, self.slot, thread_id)

    def note_error(self) -> None:
        """Drop the conversation's session after a mid-flight failure (typed reset)."""
        if self.engaged and self.conversation is not None and self.session_key is not None:
            _REGISTRY.note_provider_error(self.session_key, self.conversation)
            _drop_binding(self.conversation)

    def note_provider_compacted(self) -> None:
        """The provider compacted this thread: abandon it; next call is a full send."""
        if self.engaged and self.conversation is not None:
            _REGISTRY.mark_reset(self.conversation, "provider_compacted")
            _drop_binding(self.conversation)

    def note_thread_lost(self) -> None:
        """The bound thread no longer exists on the SDK runtime (e.g. a respawn)."""
        if self.engaged and self.conversation is not None:
            _REGISTRY.mark_reset(self.conversation, "session_evicted")
            _drop_binding(self.conversation)


def resolve_codex_send(
    *,
    messages: list[dict[str, Any]],
    model: str,
    cwd: str | None,
    effort: str | None,
    call_index: int = 0,
    audit: Callable[..., None] | None = None,
) -> CodexSend:
    """Plan one Codex SDK call: continue the conversation's thread or open a new one.

    Inert (a plain full send on an ephemeral thread, as before this module) unless a
    ReAct loop's per-forward scope, a GACT session and a react scope are all active.

    Args:
        messages: The rendered chat-message dicts for this call.
        model: The clean model id (part of the session key).
        cwd: The transport cwd (part of the session key).
        effort: The reasoning effort (part of the session key).
        call_index: Provider call index, for the audit row.
        audit: Optional audit sink override (tests); defaults to ``stream_audit``.

    Returns:
        The :class:`CodexSend` plan.
    """
    forward_scope = active_stateful_scope()
    conversation = _conversation_token() if forward_scope is not None else None
    if forward_scope is None or conversation is None:
        return CodexSend(messages=list(messages), mode="full", reason=None, engaged=False)

    with _BINDINGS_LOCK:
        _FORWARD_CONVERSATIONS.setdefault(forward_scope, set()).add(conversation)

    session_key = (conversation, model, cwd, effort or "")
    plan, slot = _REGISTRY.plan(
        session_key=session_key, scope_token=conversation, messages=messages
    )
    thread_id: str | None = None
    mode, reason = plan.mode, plan.reason
    if plan.mode == "delta":
        thread_id = _bound_thread(conversation, slot)
        if thread_id is None:
            # The registry holds the conversation but no thread was ever bound to
            # this slot (the opening send failed before the SDK reported a thread).
            _REGISTRY.mark_reset(conversation, "session_evicted")
            plan, slot = _REGISTRY.plan(
                session_key=session_key, scope_token=conversation, messages=messages
            )
            mode, reason = plan.mode, plan.reason
    send = CodexSend(
        messages=list(plan.messages),
        mode=mode,
        reason=reason,
        engaged=True,
        thread_id=thread_id if mode == "delta" else None,
        conversation=conversation,
        slot=slot,
        session_key=session_key,
    )
    _audit(
        send,
        model=model,
        call_index=call_index,
        prefix_len=plan.prefix_len,
        total=len(messages),
        sink=audit,
    )
    return send


def _audit(
    send: CodexSend,
    *,
    model: str,
    call_index: int,
    prefix_len: int,
    total: int,
    sink: Callable[..., None] | None,
) -> None:
    """Emit one ``provider.stateful`` audit row (same shape as the Claude leg)."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if sink is None:
        if not stream_audit_enabled():
            return
        sink = stream_audit
    row: dict[str, Any] = {
        "provider": "codex_sdk",
        "transport": "sdk",
        "model": f"codex_sdk/{model}",
        "call_id": send.call_id,
        "call_index": call_index,
        "stateful_mode": send.mode,
        "prefix_messages": prefix_len,
        "total_messages": total,
        "sent_messages": len(send.messages),
        "conversation": send.conversation,
        "thread_id": send.thread_id or "",
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    sink("provider.stateful", **row)


def _reset_for_tests() -> None:
    """Drop all registry + binding state in place (test isolation)."""
    _REGISTRY.reset_for_tests()
    with _BINDINGS_LOCK:
        _BINDINGS.clear()
        _FORWARD_CONVERSATIONS.clear()
        _TO_ARCHIVE.clear()
