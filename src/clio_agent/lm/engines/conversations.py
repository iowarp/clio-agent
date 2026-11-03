"""What a stateful engine's kept conversation is keyed by, and what extends it.

A provider that holds a conversation open (a Codex WebSocket response chain, a Claude
Code CLI client) is sent only what it has not seen. Each engine keeps its own
conversations where they live: Codex in its direct engine's map, Claude Code on the
pooled client that holds it (:mod:`clio_agent.providers.claude_code_conversation`).
This module holds the two rules they share:

* :func:`conversation_key` -- ``(session, run-keyed scope, *identity)`` inside an agent
  loop (an active stateful scope with a GACT session and a react scope), else ``None``
  (a one-shot call, never kept);
* :func:`new_messages` -- the messages a request adds after what the provider holds and
  its own reply, or ``None`` when the request does not extend it.
"""

from __future__ import annotations

from dspy.lm15 import Message, Request

from clio_agent.providers.stateful_common import active_stateful_scope

__all__ = ["conversation_key", "new_messages"]

Key = tuple[str, ...]


def new_messages(
    held: tuple[Message, ...], held_system: str, request: Request, system: str
) -> tuple[Message, ...] | None:
    """The messages after what the provider holds and its own reply, else ``None``.

    The provider holds what it was sent (``held``) plus the reply it produced; the next
    call must repeat both, then add only tool results / user / developer messages.
    Anything else (an edit, a different system prompt) is not a delta.
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
