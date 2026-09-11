"""Observer lifecycle integration for declared tool results and stream state."""

from __future__ import annotations

import ast
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.gact.tool_result_presentation import ToolPresentation

logger = logging.getLogger(__name__)


def _human_execution_error(error: str) -> str:
    """Project a structured execution failure to a useful primary message.

    The unmodified error remains on the tool invocation for technical details.
    """

    payload_text = re.sub(r"^\s*\d{3}:\s*", "", error.strip())
    payload: Any = None
    for loader in (json.loads, ast.literal_eval):
        try:
            payload = loader(payload_text)
        except (ValueError, SyntaxError):
            continue
        break
    if not isinstance(payload, Mapping):
        return error.strip()
    envelope = payload.get("error", payload)
    if not isinstance(envelope, Mapping):
        return error.strip()
    code = str(envelope.get("error") or "")
    details = envelope.get("details", {})
    detail_map = details if isinstance(details, Mapping) else {}
    if code == "memory_policy_denied":
        if detail_map.get("policy_decision") == "deny_other_workspace":
            return (
                "This session belongs to another workspace and is not accessible "
                "from the current workspace."
            )
        return "This memory request is outside the permitted session and workspace scope."
    message = str(envelope.get("message") or "").strip()
    return message or error.strip()


def _result_error_message(result: Any) -> str:
    """Return a tool-declared human message without exposing its envelope."""

    if not isinstance(result, Mapping):
        return ""
    message = result.get("message") or result.get("detail")
    if isinstance(message, str) and message.strip():
        return message.strip()
    envelope = result.get("error")
    if isinstance(envelope, Mapping):
        nested = envelope.get("message") or envelope.get("detail")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


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
    error: str | None = None,
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
            presentation["status"] = "failed"
            presentation["summary"] = ""
            if not any(block.get("severity") == "error" for block in presentation["blocks"]):
                presentation["blocks"].append(
                    {
                        "id": "execution-error",
                        "type": "text",
                        "label": "Request failed",
                        "severity": "error",
                        "text": _result_error_message(result) or _human_execution_error(error),
                    }
                )
        if terminal_output:
            for block in presentation["blocks"]:
                if block["type"] == "terminal":
                    block["text"] = terminal_output
    except Exception:
        logger.exception("Tool presentation failed: %s", name)
        presentation = {"summary": "", "blocks": [], "diagnostic": "presentation_failed"}
    return result, presentation
