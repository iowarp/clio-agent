"""Bounded model-lane projection of complete MCP results."""

from __future__ import annotations

import json

MAX_MODEL_TOOL_RESULT_CHARS = 12_000
MODEL_TOOL_RESULT_TRUNCATED_REASON = "model_tool_result_oversize"


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
        envelope of at most :data:`MAX_MODEL_TOOL_RESULT_CHARS` characters.
    """

    if len(text) <= MAX_MODEL_TOOL_RESULT_CHARS:
        return text
    marker_budget = 640
    preview_budget = MAX_MODEL_TOOL_RESULT_CHARS - marker_budget
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
        if len(encoded) <= MAX_MODEL_TOOL_RESULT_CHARS:
            best = encoded
            low = preview + 1
        else:
            high = preview - 1
    return best
