"""The streamed side of one ``ClioReAct`` LM call: live lanes, reply split, kill handle.

* :func:`route_pieces` -- a chunk's pieces (routed by
  :class:`~clio_agent.gact.reasoning_extract.StreamRouter`) to the live thinking and
  answer lanes; thinking rides ``provider_thinking:model:<origin>`` so its transcript
  part records whether it was the provider's field or an inline ``<think>`` block;
* :func:`reply_thinking_and_text` -- the typed reply split by the SAME rule, so what the
  step records (and a reload shows) equals what streamed;
* :func:`register_stream` -- makes the in-flight HTTP call killable by the session
  cancel (``provider_streams_killed`` counts it).
"""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
from dspy.lm15 import Response, TextPart, ThinkingPart

from clio_agent.gact.reasoning_extract import (
    PROVIDER_FIELD,
    THINKING,
    Piece,
    StreamRouter,
    split_leading_think,
)

__all__ = [
    "StreamRouter",
    "register_stream",
    "reply_thinking_and_text",
    "route_pieces",
    "unregister_stream",
]

#: Engines that register their own kill handle for a session cancel (no double count).
_SELF_REGISTERING_MODELS = ("claude_code/",)


def route_pieces(pieces: list[Piece], *, unclosed: bool = False) -> None:
    """Send one chunk's routed pieces to the live lanes."""
    from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
        note_lm_activity,
        note_lm_answer_delta,
        note_lm_provider_thinking_delta,
        note_lm_token_event,
    )

    note_lm_activity()
    text = thinking = ""
    for piece in pieces:
        if piece.kind == THINKING:
            note_lm_provider_thinking_delta(piece.text, provider=f"model:{piece.source}")
            thinking += piece.text
        else:
            note_lm_answer_delta(piece.text, field="next_thought")
            text += piece.text
    if text or thinking:
        note_lm_token_event(text, thinking, field="next_thought")
    if unclosed:
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        stream_audit("reasoning.inline_unclosed", reason="stream ended inside <think>")


def reply_thinking_and_text(
    response: Response,
) -> tuple[list[ThinkingPart], str, tuple[str, bool]]:
    """A reply's thinking and visible text, by the live stream's rule (reload parity).

    Thinking the provider sent as its own field wins and the text is kept as written;
    otherwise a leading inline ``<think>`` block becomes the thinking. The origin is
    ``(source, complete)``: ``source`` is ``provider_field``, ``inline_tag`` or ``""``.
    """
    thinking = [p for p in response.message.parts if isinstance(p, ThinkingPart)]
    text = "".join(p.text for p in response.message.parts if isinstance(p, TextPart))
    if any(t.text for t in thinking):
        return thinking, text, (PROVIDER_FIELD, True)
    split = split_leading_think(text)
    if not split.source:
        return thinking, text, ("", True)
    if split.thinking.strip():
        thinking = [*thinking, ThinkingPart(text=split.thinking)]
    return thinking, split.answer, (split.source, split.complete)


def register_stream(lm: Any, session_id: str, scope: anyio.CancelScope) -> Any:
    """Make an in-flight HTTP call killable by the session cancel; its handle or ``None``.

    ``abort_session_streams`` (the cancel route, another thread) fires the handle: it
    cancels the call's scope on the LM loop, closing the HTTP stream at once (the
    call's 0.25 s cancel watcher stays the fallback), and ``provider_streams_killed``
    counts it. Called on the LM loop.
    """
    if not session_id or str(getattr(lm, "model", "")).startswith(_SELF_REGISTERING_MODELS):
        return None
    from clio_agent.providers.claude_code_cancel import register_sdk_stream  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    return register_sdk_stream(session_id, lambda: loop.call_soon_threadsafe(scope.cancel))


def unregister_stream(handle: Any) -> None:
    """Drop the call's handle when it ends (any way)."""
    if handle is not None:
        from clio_agent.providers.claude_code_cancel import unregister_sdk_stream  # noqa: PLC0415

        unregister_sdk_stream(handle)
