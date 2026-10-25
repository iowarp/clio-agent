"""Authorized paging of retained transcript and correlated task presentation."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from clio_agent.gact.tool_execution_projection import execution_presentation
from clio_agent.gact.tool_result_presentation import presentation_page, recorded_presentation


def tool_presentation_content(
    app: Any,
    session_id: str,
    call_id: str,
    block_id: str,
    cursor: int,
    result_sha256: str | None = None,
) -> dict[str, Any]:
    """Read the actual owning content and reject a continuation for changed result bytes."""
    parts = [part for message in app.state.messages.get(session_id, []) for part in message.parts]
    parts.extend(getattr(app.state, "live_assistant_parts", {}).get(session_id, []))
    call = next(
        (part for part in parts if part.call_id == call_id and part.type == "tool_call"), None
    )
    if call is None:
        call = next(
            (part for part in parts if part.call_id == call_id and part.type == "tool_result"), None
        )
    if call is None:
        raise HTTPException(status_code=404, detail="Tool invocation not found in session")
    presentations = []
    if block_id.startswith("task-result-"):
        try:
            presentation = execution_presentation(
                app,
                session_id,
                call_id,
                call.tool_name or "",
                call.input,
                result_sha256=result_sha256,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if presentation is not None:
            presentations.append(presentation)
    else:
        presentations.extend(
            recorded_presentation(part.model_dump(), call.input) or {}
            for part in reversed(parts)
            if part.call_id == call_id
        )
    for presentation in presentations:
        for block in presentation.get("blocks", []):
            if block["id"] == block_id:
                try:
                    return presentation_page(block, cursor)
                except ValueError as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
    raise HTTPException(status_code=404, detail="Presentation content not found in session")
