"""Durable user navigation marks, independent of the model's conversation context."""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import Any

from fastapi import FastAPI, HTTPException

MARKS_KEY = "message_marks"


def _mark_patch(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key or type(marked) is not bool
        for key, marked in value.items()
    ):
        raise ValueError("message_marks must map message IDs to booleans.")
    return value


def saved_message_marks(
    metadata: Mapping[str, Any], message_ids: Set[str] | None = None
) -> dict[str, bool]:
    """Read active marks, optionally restricted to a copied transcript boundary."""
    marks = metadata.get(MARKS_KEY)
    if not isinstance(marks, dict):
        return {}
    return {
        key: True
        for key, marked in marks.items()
        if isinstance(key, str) and marked is True and (message_ids is None or key in message_ids)
    }


def merge_session_metadata(metadata: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mark changes individually so clients cannot overwrite unrelated marks.

    A true value marks a message; false removes its mark. Other metadata retains
    the session store's existing shallow-merge semantics. Call under its lock.
    """
    merged = {**metadata, **patch}
    if MARKS_KEY in patch:
        marks = saved_message_marks(metadata)
        for message_id, marked in _mark_patch(patch[MARKS_KEY]).items():
            if marked:
                marks[message_id] = True
            else:
                marks.pop(message_id, None)
        merged[MARKS_KEY] = marks
    return merged


def validated_session_metadata(
    app: FastAPI, session_id: str, metadata: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Validate a client's mark patch before any session fields are changed.

    Keys only the server sets (``agent_flow``, ``unattended``, ...) are refused, typed.
    """
    from clio_agent.gact.session_run_setup import refuse_reserved_metadata  # noqa: PLC0415

    refuse_reserved_metadata(metadata)
    if metadata is None or MARKS_KEY not in metadata:
        return metadata
    try:
        marks = _mark_patch(metadata[MARKS_KEY])
    except ValueError as error:
        raise HTTPException(
            422,
            detail={
                "error": {
                    "error": "bad_request",
                    "message": str(error),
                }
            },
        ) from error
    message_ids = {message.id for message in app.state.messages.get(session_id, [])}
    if missing := set(marks) - message_ids:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "error": "not_found",
                    "message": "Marked message not found in this conversation.",
                    "details": {"message_ids": sorted(missing)},
                }
            },
        )
    return metadata
