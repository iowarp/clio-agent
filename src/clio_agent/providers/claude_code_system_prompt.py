"""Split the system message out of a chat transcript (B4, Claude SDK tuning).

Before this, every ``claude_code`` call serialized the WHOLE message list --
including CLIO's rendered system instructions -- into the single prompt string
sent as the SDK query's ``input`` (``claude_code_litellm.py:127-138`` via
``messages_to_prompt``). ``ClaudeAgentOptions.system_prompt`` is the SDK's own
first-class field for this: setting it drops Claude Code's own persona/preamble
(B4's actual goal) and keeps the system instructions out of the conversational
input, where they would otherwise occupy space in every delta/full send.

This module owns exactly the split -- pulling ``role == "system"`` messages out
of an OpenAI-shape message list -- so :mod:`clio_agent.providers.claude_code_litellm`
can pass the extracted text as ``system_prompt`` and serialize only the
REMAINING (non-system) messages into the query input. ``messages_to_prompt``
itself (:mod:`clio_agent.providers._cli_provider`, shared with the ``codex``
provider) is untouched -- called directly it still serializes every role,
system included, for callers (existing unit tests, the codex provider) that
want that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clio_agent.providers._cli_provider import normalise_message_content

__all__ = ["prepare_claude_request", "split_system_prompt"]


def split_system_prompt(
    messages: list[dict[str, Any]],
    *,
    unsupported_multimodal_exc: type[Exception],
    transport_label: str = "Claude Code",
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(system_prompt_text, remaining_messages)``.

    Every message whose ``role`` is (case-insensitively) ``"system"`` is
    removed from the list, its content normalised to text (reusing the shared
    CLI-provider text coercion so an image part in a system message is refused
    exactly like it would be in a user turn, not silently dropped), and joined
    with blank lines in their original order. Messages of every other role are
    returned unchanged and in their original relative order.

    An empty result (``""``) means no system message was present -- the caller
    should then omit ``system_prompt`` entirely (``None``) rather than pass an
    empty string, so the SDK/CLI's own default applies.

    Args:
        messages: OpenAI-shape message dicts.
        unsupported_multimodal_exc: Provider exception raised on an image part
            inside a system message (mirrors ``messages_to_prompt``'s contract).
        transport_label: Human label used in a raised error message.

    Returns:
        The extracted system text (``""`` if none) and the remaining messages.
    """
    system_parts: list[str] = []
    remaining: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "")).strip().lower()
        if role != "system":
            remaining.append(message)
            continue
        text = normalise_message_content(
            message.get("content", ""),
            unsupported_multimodal_exc=unsupported_multimodal_exc,
            transport_label=transport_label,
        )
        if text:
            system_parts.append(text)
    return "\n\n".join(system_parts), remaining


def prepare_claude_request(
    messages: list[dict[str, Any]],
    *,
    serialize_text: Callable[[list[dict[str, Any]]], str],
    unsupported_multimodal_exc: type[Exception],
    transport_label: str = "Claude Code",
) -> tuple[str, str, list[dict[str, Any]]]:
    """Split the system prompt out, then extract native blocks + serialize the rest.

    The one-stop call site :mod:`claude_code_litellm` uses at every entry
    point: ``(system_prompt, prompt, native_blocks) = prepare_claude_request(...)``.
    ``system_prompt`` is ``""`` when the caller sent no system message —
    callers pass ``None`` (not ``""``) on to ``ClaudeAgentOptions`` in that
    case, so the CLI's own default applies.
    """
    from clio_agent.providers.claude_code_multimodal import (  # noqa: PLC0415
        messages_to_claude_input,
    )

    system_prompt, remaining = split_system_prompt(
        messages,
        unsupported_multimodal_exc=unsupported_multimodal_exc,
        transport_label=transport_label,
    )
    prompt, native_blocks = messages_to_claude_input(
        remaining,
        serialize_text=serialize_text,
        unsupported_multimodal_exc=unsupported_multimodal_exc,
    )
    return system_prompt, prompt, native_blocks
