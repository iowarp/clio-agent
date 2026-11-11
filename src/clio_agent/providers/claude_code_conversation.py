"""The conversation a pooled Claude Code client holds, and how a request continues it.

One ``ClaudeSDKClient`` holds one conversation, whatever a query's ``session_id`` says
(probe, 2026-11-03: a query under ``conv-b`` recalled a word given under ``conv-a``).
So the conversation lives on the pooled entry that owns the client
(:class:`~clio_agent.providers.claude_code_sessions._StreamClientEntry`), not in a
separate registry keyed apart from it. Before this module there were two models of a
conversation: a registry keyed by (session, scope, model, cwd, thinking), and a pool
keyed by session. A "new" conversation was appended to the old history, and compaction
never shrank the context (``docs/design/codebase-audit-2026-11.md``).

:func:`plan` decides, against what the entry holds, whether a request continues it
(only the new messages are sent) or starts it over (a full send; the entry clears the
CLI first, :mod:`~clio_agent.providers.claude_code_clear`). A full send inside an
agent loop carries a typed reason from
:data:`~clio_agent.providers.stateful_common.STATEFUL_RESET_REASONS`. Calls outside an
agent loop run on the session's side client and are always full and never kept.

An ARC op that rewrites the history (compaction) resets every conversation its forward
drove (:class:`_ForwardResets`, registered with
:func:`~clio_agent.providers.stateful_common.register_scope_registry`).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from dspy.lm15 import Message, Request

from clio_agent.lm.engines.conversations import new_messages
from clio_agent.providers.stateful_common import active_stateful_scope, register_scope_registry

__all__ = ["HeldConversation", "Send", "forward_resets", "plan", "record_sent"]


@dataclass(frozen=True)
class HeldConversation:
    """What the CLI behind one pooled client holds."""

    identity: tuple[str, ...]  # (model, cwd, thinking): another identity starts over
    handle: str  # the SDK session label this conversation's queries carry (cost)
    system: str
    held: tuple[Message, ...]  # the messages it was sent (its replies come after each)


@dataclass
class Send:
    """One call's plan: the messages to send and whether they continue the conversation."""

    kept: bool  # inside an agent loop: the conversation is kept after this call
    messages: tuple[Message, ...]
    handle: str | None  # the conversation continued; ``None`` starts one (a full send)
    reason: str | None  # the typed reason of a full send inside a loop
    identity: tuple[str, ...] = ()
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def plan(
    entry: Any, request: Request, system: str, identity: tuple[str, ...], *, kept: bool
) -> Send:
    """Plan ``request`` on ``entry``: a delta when it extends what the CLI holds.

    ``kept``: the call belongs to an agent loop (its conversation is kept); otherwise
    it is a one-shot call on the session's side client, always full, never kept.
    """
    if not kept:
        return Send(False, request.messages, None, None, identity)
    if (forward := active_stateful_scope()) is not None:
        _FORWARDS.drove(forward, entry)
    pending, entry.pending_reset = entry.pending_reset, None
    held: HeldConversation | None = entry.conversation
    if held is None:
        return Send(True, request.messages, None, pending or "first_call", identity)
    new = new_messages(held.held, held.system, request, system)
    if pending is not None or held.identity != identity or new is None:
        entry.conversation = None
        return Send(True, request.messages, None, pending or "prefix_mismatch", identity)
    return Send(True, new, held.handle, None, identity)


def record_sent(entry: Any, send: Send, handle: str, request: Request, system: str) -> str | None:
    """After a successful kept call: the entry now holds ``request``'s messages.

    Returns the handle of a conversation this one replaced (its cost tally is over).
    """
    if send.handle is not None and entry.conversation is not None:
        entry.conversation = HeldConversation(
            send.identity, send.handle, entry.conversation.system, request.messages
        )
        return None
    previous = entry.conversation.handle if entry.conversation is not None else None
    entry.conversation = HeldConversation(send.identity, handle, system, request.messages)
    return previous


def reset(entry: Any, reason: str) -> None:
    """Forget the entry's conversation: the next kept call is a full send with ``reason``."""
    entry.conversation = None
    entry.pending_reset = reason


class _ForwardResets:
    """The entries each forward drove, so an ARC op can reset them all."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._forwards: dict[str, list[Any]] = {}

    def drove(self, forward: str, entry: Any) -> None:
        with self._lock:
            entries = self._forwards.setdefault(forward, [])
            if not any(e is entry for e in entries):
                entries.append(entry)

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None:
        with self._lock:
            entries = list(self._forwards.get(scope_token, ()))
        for entry in entries:
            reset(entry, reason)

    def release(self, scope_token: str) -> None:
        with self._lock:
            self._forwards.pop(scope_token, None)

    def clear_for_tests(self) -> None:
        with self._lock:
            self._forwards.clear()


_FORWARDS = _ForwardResets()
register_scope_registry(_FORWARDS)


def forward_resets() -> _ForwardResets:
    """The process-wide forward-to-entries map (tests clear it)."""
    return _FORWARDS
