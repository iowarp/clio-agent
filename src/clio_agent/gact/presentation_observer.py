"""Observer lifecycle integration for declared tool results and stream state."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.gact.tool_result_presentation import ToolPresentation

logger = logging.getLogger(__name__)


def starting_presentation(name: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve native or MCP running presentation at the observer-only boundary."""
    from clio_agent.gact.agents.tool_instrumentation import present_native_start
    from clio_agent.tools.tool_presentation import starting_presentation as mcp_start

    native = present_native_start(name, args)
    return native if native is not None else mcp_start(name, args)


def publish_presentation_delta(app: Any, sid: str, payload: dict[str, Any]) -> None:
    """Retain an append in the live snapshot and publish its ordered delta."""
    delta = payload.get("presentation_delta")
    if delta is None:
        return
    for part in getattr(app.state, "live_assistant_parts", {}).get(sid, []):
        if part.call_id == payload["call_id"] and part.type == "tool_call":
            for block in (part.presentation or {}).get("blocks", []):
                if block["id"] == delta["block_id"]:
                    block["text"] = payload["output_stream"]
    app.state.bus.publish(Event(type="tool.presentation.delta", session_id=sid, payload=delta))


def completed_presentation(
    name: str,
    args: Mapping[str, Any],
    result: Any,
    structured: Any,
    terminal_output: str,
    *,
    error: str = "",
) -> tuple[Any, dict[str, Any]]:
    """Prepare the observer view; failures cannot alter the tool's result or status."""
    from clio_agent.gact.agents.tool_instrumentation import present_native_result
    from clio_agent.tools.tool_presentation import present_mcp_result

    try:
        presentation = result.get("presentation") if isinstance(result, Mapping) else None
        if presentation is not None:
            result = {key: value for key, value in result.items() if key != "presentation"}
        if presentation is None:
            presentation = present_native_result(name, args, result, structured)
        if presentation is None:
            presentation = present_mcp_result(name, args, result)
        presentation = ToolPresentation.model_validate(presentation).model_dump(exclude_none=True)
        if error:
            presentation["blocks"].append({"id": "execution-error", "type": "text", "text": error})
        if terminal_output:
            for block in presentation["blocks"]:
                if block["type"] == "terminal":
                    block["text"] = terminal_output
    except Exception:
        logger.exception("Tool presentation failed: %s", name)
        presentation = {"summary": "", "blocks": [], "diagnostic": "presentation_failed"}
    return result, presentation
