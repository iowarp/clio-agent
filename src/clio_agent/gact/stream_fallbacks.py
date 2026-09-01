"""Typed live-stream fallback ledger."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.capabilities import _STREAM_FALLBACK_REASON_DEFINITIONS

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def stream_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build structured metadata for a degraded live-delivery path."""

    definition = _STREAM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stream fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def stream_fallback_reasons(app: "FastAPI") -> dict[str, dict[str, Any]]:
    reasons = getattr(app.state, "stream_fallback_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.stream_fallback_reasons = reasons
    return reasons


def record_stream_fallback(app: "FastAPI", sid: str, reason: str, message: str = "") -> None:
    payload = stream_fallback_payload(reason, message)
    stream_fallback_reasons(app)[sid] = payload
    logger.warning(
        "live delivery degraded session_id=%s reason=%s message=%s",
        sid,
        reason,
        message or payload.get("description", ""),
    )


def peek_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    """Read a turn's live-delivery degradation without consuming it."""

    return dict(stream_fallback_reasons(app).get(sid, {}))


def pop_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    return stream_fallback_reasons(app).pop(sid, {})
