"""The streamed side of one ``ClioReAct`` LM call: live lanes and the reply split.

* :func:`route_pieces` -- a chunk's pieces (routed by
  :class:`~clio_agent.gact.reasoning_extract.StreamRouter`) to the live thinking and
  answer lanes; thinking rides ``provider_thinking:model:<origin>`` so its transcript
  part records whether it was the provider's field or an inline ``<think>`` block;
* :func:`reply_thinking_and_text` -- the typed reply split by the SAME rule, so what the
  step records (and a reload shows) equals what streamed.
"""

from __future__ import annotations

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
    "reply_thinking_and_text",
    "route_pieces",
]


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
