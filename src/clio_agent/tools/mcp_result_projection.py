"""Bounded model-lane projection of complete MCP results.

This bounds the text that enters the MODEL's context. It is deliberately a
DIFFERENT knob from ``limits.tool_result_chars``
(:func:`clio_agent.gact.evidence._bounded_tool_call_result`), which bounds the
preview stored in assistant metadata and shipped to the transcript UI: the two
lanes have different consumers and different failure modes (context budget vs
transcript payload size), so each owns exactly one key and neither can silently
redirect the other. The raw evidence itself is never rewritten by either.
"""

from __future__ import annotations

import json

MODEL_TOOL_RESULT_TRUNCATED_REASON = "model_tool_result_oversize"

#: Characters the truncation envelope reserves for its own JSON scaffolding
#: (status/reason/counters plus the ``head``/``tail`` keys), so the preview
#: budget is what is left of the resolved bound after the marker.
_MARKER_BUDGET_CHARS = 640


def model_tool_result_chars() -> int:
    """Character bound on the MODEL-facing projection of one MCP tool result.

    Config: ``limits.model_tool_result_chars`` /
    ``CLIO_MODEL_TOOL_RESULT_CHARS`` (default 12000). Lower it to protect a
    small context window from one verbose tool; raise it when a model has room
    and truncation is costing the agent evidence it needs.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "limits.model_tool_result_chars",
        env="CLIO_MODEL_TOOL_RESULT_CHARS",
        default=12_000,
        cast=conf.as_int,
    )


def _encode_bounded(text: str, head_chars: int, tail_chars: int) -> str:
    """Encode one truncation envelope, stamping the ACTUAL slice lengths.

    Args:
        text: The complete result text being bounded.
        head_chars: Characters to keep from the start.
        tail_chars: Characters to keep from the end.

    Returns:
        The JSON-encoded envelope.
    """

    head = text[:head_chars]
    tail = text[len(text) - tail_chars :] if tail_chars else ""
    bounded = {
        "_clio": {
            "status": "truncated",
            "reason": MODEL_TOOL_RESULT_TRUNCATED_REASON,
            "original_chars": len(text),
            "head_chars": len(head),
            "tail_chars": len(tail),
        },
        "head": head,
        "tail": tail,
    }
    return json.dumps(bounded, ensure_ascii=False)


def bounded_model_tool_result(text: str) -> str:
    """Bound model-facing text while leaving raw evidence unchanged.

    The slices are re-escaped by ``json.dumps`` after they are cut, and a quote-
    or control-character-dense payload can grow by 2x-6x in that step. Slicing to
    the budget and encoding once therefore does not bound anything: the budget is
    spent on the pre-escape text. The envelope is instead measured AFTER encoding
    and the preview shrunk until the encoded result fits.

    Args:
        text: The complete model-facing result text.

    Returns:
        ``text`` unchanged when it already fits, otherwise a typed truncation
        envelope of at most :func:`model_tool_result_chars` characters.
    """

    max_chars = model_tool_result_chars()
    if len(text) <= max_chars:
        return text
    # The marker budget is DERIVED from the resolved bound, never a second
    # independent literal: a lowered bound shrinks the preview with it.
    preview_budget = max_chars - _MARKER_BUDGET_CHARS
    # Encoded length grows monotonically with the preview size, so binary-search
    # the largest 75/25 preview whose ENCODED envelope still fits. An empty
    # preview always fits (the envelope alone is ~160 characters), which makes
    # the search total and keeps the loop bounded at ~log2(preview_budget) passes.
    best = _encode_bounded(text, 0, 0)
    low, high = 1, preview_budget
    while low <= high:
        preview = (low + high) // 2
        head_chars = (preview * 3) // 4
        encoded = _encode_bounded(text, head_chars, preview - head_chars)
        if len(encoded) <= max_chars:
            best = encoded
            low = preview + 1
        else:
            high = preview - 1
    return best
