"""Separate the immutable human transcript from the compacted model projection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def model_context_messages(messages: Iterable[Any]) -> list[Any]:
    """Return the working model context beginning at the latest compaction marker.

    The supplied collection remains the complete human transcript. A compaction part
    is a checkpoint for prompt materialization only; it never deletes earlier rows.
    """

    rows = list(messages)
    start = 0
    for index, message in enumerate(rows):
        parts = getattr(message, "parts", None)
        if parts is None and isinstance(message, dict):
            parts = message.get("parts", [])
        for part in parts or []:
            part_type = getattr(part, "type", None)
            if part_type is None and isinstance(part, dict):
                part_type = part.get("type")
            if part_type == "compaction":
                start = index
                break
    return rows[start:]
