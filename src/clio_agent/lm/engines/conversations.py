"""Kept provider conversations for the stateful engines (Codex direct, Claude Code).

A provider that holds a conversation open (a Codex WebSocket response chain, a
Claude Code session) is sent only what it has not seen. :class:`ConversationRegistry` remembers,
per conversation key, the provider's handle (thread or session id), the system prompt
it was opened with and the messages it was sent. A call whose messages repeat all of
that plus the provider's own reply, then add only non-assistant messages, continues the
conversation with those new messages; anything else opens a new one and sends in full,
with a typed reset reason (``first_call`` / ``prefix_mismatch`` / ``session_evicted``
/ ``provider_error`` / ``provider_compacted`` / ``ops_reset``).

Keys exist only inside an agent loop (an active stateful scope with a GACT session and
a react scope): ``(session, run-keyed scope, *engine identity)``. Conversations outlive
the forward that opened them (a later turn of the same agent continues them); an ARC
op on a forward resets every conversation that forward drove. Superseded handles are
queued for the engine to release (:meth:`ConversationRegistry.take_released`).
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from dspy.lm15 import Message, Request

from clio_agent.providers.stateful_common import active_stateful_scope, register_scope_registry

__all__ = ["ConversationRegistry", "Send", "conversation_key", "new_messages"]

Key = tuple[str, ...]


@dataclass
class _Conversation:
    handle: str
    system: str
    held: tuple[Message, ...]  # the messages the provider was sent


@dataclass
class Send:
    """One call's plan: the messages to send and the conversation it continues."""

    key: Key | None
    messages: tuple[Message, ...]
    handle: str | None  # the kept conversation to continue; None opens a new one
    reason: str | None  # the typed reset reason of a full send inside a loop
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class ConversationRegistry:
    """Process-wide registry of kept conversations (bounded LRU, thread-safe)."""

    def __init__(self, capacity: Callable[[], int]) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._live: OrderedDict[Key, _Conversation] = OrderedDict()
        self._resets: dict[Key, str] = {}
        self._forward: dict[str, set[Key]] = {}
        self._released: list[str] = []
        register_scope_registry(_ScopeAdapter(self))

    def plan(self, key: Key | None, request: Request, system: str) -> Send:
        """Decide whether ``request`` continues the kept conversation for ``key``."""
        if key is None:
            return Send(key=None, messages=request.messages, handle=None, reason=None)
        forward = active_stateful_scope()
        with self._lock:
            if forward is not None:
                self._forward.setdefault(forward, set()).add(key)
            pending = self._resets.pop(key, None)
            live = self._live.get(key)
            if live is None:
                return Send(key, request.messages, None, pending or "first_call")
            new = new_messages(live.held, live.system, request, system)
            if pending is not None or new is None:
                self._drop(key)
                return Send(key, request.messages, None, pending or "prefix_mismatch")
            self._live.move_to_end(key)
            return Send(key, new, live.handle, None)

    def opened(self, key: Key, handle: str, request: Request, system: str) -> None:
        """Record a new conversation that was sent ``request`` in full."""
        with self._lock:
            self._drop(key)
            self._live[key] = _Conversation(handle, system, request.messages)
            while len(self._live) > self._capacity():
                _old, conversation = self._live.popitem(last=False)
                self._released.append(conversation.handle)

    def continued(self, key: Key, request: Request) -> None:
        """Record that the kept conversation now holds ``request``'s messages."""
        with self._lock:
            live = self._live.get(key)
            if live is not None:
                live.held = request.messages

    def reset(self, key: Key, reason: str) -> None:
        """Drop the conversation; the next call sends in full with ``reason``."""
        with self._lock:
            self._drop(key)
            self._resets[key] = reason

    def reset_session(self, session: str, reason: str) -> None:
        """Reset every conversation of the GACT session ``session`` (its client is gone)."""
        with self._lock:
            keys = [key for key in self._live if key[0] == session]
        for key in keys:
            self.reset(key, reason)

    def reset_forward(self, forward: str, reason: str) -> None:
        """Reset every conversation the forward ``forward`` drove."""
        with self._lock:
            keys = set(self._forward.get(forward, set()))
        for key in keys:
            self.reset(key, reason)

    def release_forward(self, forward: str) -> None:
        """Forget the forward's key set (its conversations stay kept)."""
        with self._lock:
            self._forward.pop(forward, None)

    def take_released(self) -> list[str]:
        """Handles superseded or evicted since the last call (for the engine to release)."""
        with self._lock:
            pending, self._released = self._released, []
            return pending

    def clear_for_tests(self) -> None:
        """Forget everything (tests only)."""
        with self._lock:
            self._live.clear()
            self._resets.clear()
            self._forward.clear()
            self._released.clear()

    def _drop(self, key: Key) -> None:
        conversation = self._live.pop(key, None)
        if conversation is not None:
            self._released.append(conversation.handle)


def new_messages(
    held: tuple[Message, ...], held_system: str, request: Request, system: str
) -> tuple[Message, ...] | None:
    """The messages after what the provider holds and its own reply, else ``None``.

    The provider holds what it was sent (``live.held``) plus the reply it produced; the
    next call must repeat both, then add only tool results / user / developer
    messages. Anything else (an edit, a different system prompt) is not a delta.
    """
    messages = request.messages
    if system != held_system or len(messages) <= len(held) or messages[: len(held)] != held:
        return None
    if messages[len(held)].role != "assistant":
        return None
    new = messages[len(held) + 1 :]
    if not new or any(m.role == "assistant" for m in new):
        return None
    return new


class _ScopeAdapter:
    """ARC ops reset every conversation a forward drove; conversations outlive it."""

    def __init__(self, registry: ConversationRegistry) -> None:
        self._registry = registry

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None:
        self._registry.reset_forward(scope_token, reason)

    def release(self, scope_token: str) -> None:
        self._registry.release_forward(scope_token)


def conversation_key(*identity: str) -> Key | None:
    """``(session, run-keyed scope, *identity)`` inside an agent loop, else ``None``."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    if active_stateful_scope() is None:
        return None
    session = (_ctx.active_session_id() or "").strip()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    if not session or not scope:
        return None
    return (session, scope, *identity)
