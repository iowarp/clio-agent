"""Provider-neutral text and reasoning extraction from streamed chunks."""

from __future__ import annotations

from typing import Any


def _chunk_reasoning_text(piece: Any) -> str:
    """Pull provider reasoning text from object and OpenAI-dict chunks."""

    if not piece or isinstance(piece, str):
        return ""
    if isinstance(piece, dict):
        try:
            delta = piece["choices"][0]["delta"]
            return str(delta.get("reasoning_content") or delta.get("reasoning") or "")
        except (KeyError, IndexError, TypeError):
            return ""
    try:
        choices = piece.choices
        if choices:
            delta = getattr(choices[0], "delta", None)
            reasoning = (
                getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if delta is not None
                else None
            )
            if reasoning:
                return str(reasoning)
    except Exception:  # noqa: BLE001,S110 - optional provider shape
        pass
    return ""


def _chunk_text(piece: Any) -> str:
    """Pull answer text from object, plain-string, and OpenAI-dict chunks."""

    if isinstance(piece, str):
        return piece
    try:
        choices = piece.choices
        if choices:
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                return str(content)
    except Exception:  # noqa: BLE001,S110 - optional provider shape
        pass
    if isinstance(piece, dict):
        try:
            return piece["choices"][0]["delta"].get("content", "") or ""
        except (KeyError, IndexError, TypeError):
            return ""
    return ""
