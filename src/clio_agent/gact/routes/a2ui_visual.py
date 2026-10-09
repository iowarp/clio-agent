"""Session-authorized renderer reports and single-owner screenshot delivery."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from clio_agent.gact.a2ui_visual import (
    A2UIVisualFeedback,
    CaptureReply,
    ViewerReport,
    VisualFeedbackError,
)


def register_a2ui_visual_routes(app: FastAPI) -> None:
    """Install the visual bridge once alongside the existing A2UI routes."""
    app.state.a2ui_visual = A2UIVisualFeedback()

    @app.post("/v1/sessions/{sid}/a2ui/visual-feedback")
    async def feedback(sid: str, request: Request) -> dict[str, Any]:
        """Report the mounted view or return its claimed capture, with bounded input."""
        if app.state.sessions.get(sid) is None:
            raise HTTPException(404, "Session not found.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 6_750_000:
                raise HTTPException(413, "Visual feedback exceeds its byte limit.")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Visual feedback must be an object.")
            if "request_id" in body:
                reply = CaptureReply.model_validate(body)
                app.state.a2ui_visual.complete(sid, reply)
                return {"accepted": True}
            report = ViewerReport.model_validate(body)
            return {"requests": app.state.a2ui_visual.report(sid, report)}
        except (ValidationError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, VisualFeedbackError) else "validation_error"
            raise HTTPException(
                409 if isinstance(exc, VisualFeedbackError) else 422,
                {"reason": reason, "detail": str(exc)},
            ) from exc
