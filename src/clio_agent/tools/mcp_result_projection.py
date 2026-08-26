"""Bounded model-lane projection of complete MCP results."""

from __future__ import annotations

import json

MAX_MODEL_TOOL_RESULT_CHARS = 12_000
MODEL_TOOL_RESULT_TRUNCATED_REASON = "model_tool_result_oversize"


def bounded_model_tool_result(text: str) -> str:
    """Bound model-facing text while leaving raw evidence unchanged."""

    if len(text) <= MAX_MODEL_TOOL_RESULT_CHARS:
        return text
    marker_budget = 640
    preview_budget = MAX_MODEL_TOOL_RESULT_CHARS - marker_budget
    head_chars = int(preview_budget * 0.75)
    tail_chars = preview_budget - head_chars
    bounded = {
        "_clio": {
            "status": "truncated",
            "reason": MODEL_TOOL_RESULT_TRUNCATED_REASON,
            "original_chars": len(text),
            "head_chars": head_chars,
            "tail_chars": tail_chars,
        },
        "head": text[:head_chars],
        "tail": text[-tail_chars:],
    }
    return json.dumps(bounded, ensure_ascii=False)
