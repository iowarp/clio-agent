"""How a call of a view's own tool (``app__<server>__<tool>``) reads in the transcript.

The row names the view tool and the view it acts on ("Set label" on "board view").
Expansion shows what the agent sent, as readable lines, and what the view returned.
A view that is not open, or that no longer offers the tool, is a failed call that says
so and names the tool that opens the view. Exact arguments stay in Technical details.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["present", "starting"]

_SUMMARY = {
    "returned": "Returned",
    "view_error": "The view reported an error",
    "not_open": "The view is not open",
    "not_offered": "The view no longer offers this tool",
}


def _title(tool: str) -> str:
    return tool.replace("_", " ").strip().capitalize() or "View tool"


def _value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def _sent(arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    if not arguments:
        return None
    lines = [f"{key}: {_value(value)}" for key, value in arguments.items()]
    return {"id": "sent", "type": "text", "label": "Sent", "text": "\n".join(lines)}


def _head(server: str) -> list[dict[str, Any]]:
    return [{"id": "view", "type": "text", "text": f"{server} view" if server else "view"}]


def _from_name(name: str) -> tuple[str, str]:
    """``app__<server>__<tool>`` -> (server, tool)."""
    rest = name[len("app__") :] if name.startswith("app__") else name
    server, _, tool = rest.partition("__")
    return server, tool or rest


def starting(server: str, tool: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """The running row of one view tool: its name, the view, and what was sent."""

    def start(args: Mapping[str, Any]) -> dict[str, Any]:
        blocks = _head(server)
        sent = _sent(args)
        if sent:
            blocks.append(sent)
        return {
            "action": _title(tool),
            "subject": "view",
            "blocks": blocks,
            "summary": "Waiting for the view",
        }

    return start


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The finished call: what was sent, what came back, and whether the view ran it."""
    row = structured if isinstance(structured, Mapping) else {}
    server = str(row.get("server") or "")
    tool = str(row.get("tool") or "")
    if not tool and isinstance(row.get("agent_name"), str):
        server, tool = _from_name(row["agent_name"])
    outcome = str(row.get("outcome") or "returned")
    text = str(row.get("text") or (result if isinstance(result, str) else ""))
    blocks = _head(server)
    sent = _sent(row.get("arguments") if isinstance(row.get("arguments"), Mapping) else args)
    if sent:
        blocks.append(sent)
    if outcome == "returned":
        blocks.append({"id": "returned", "type": "text", "label": "Returned", "text": text})
    else:
        blocks.append({"id": "outcome", "type": "text", "severity": "warning", "text": text})
    view: dict[str, Any] = {
        "action": _title(tool),
        "subject": "view",
        "blocks": blocks,
        "summary": _SUMMARY.get(outcome, "Returned"),
    }
    if outcome != "returned":
        view["status"] = "failed"
    return view
