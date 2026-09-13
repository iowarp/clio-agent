"""MCP Apps tool UI metadata + model-visibility gate (#1333 ratchet payment).

Split out of ``mcp_executor.py``: reading a FastMCP tool's normalized ``_meta.ui``
block, and deciding whether a tool belongs on the model-facing surface from its
declared ``visibility`` scopes. ``mcp_executor.py`` and ``tools/execution.py``
both call :func:`_tool_visible_to_model` to filter their tool tables; it stays
importable from ``mcp_executor`` (re-exported there) so neither caller's import
path changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _tool_ui_metadata(tool: Any) -> Mapping[str, Any]:
    """Return normalized MCP Apps metadata from a FastMCP tool definition."""

    if tool is None:
        return {}
    meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None)
    if meta is None and isinstance(tool, Mapping):
        meta = tool.get("_meta") or tool.get("meta")
    meta_dump = getattr(meta, "model_dump", None)
    if callable(meta_dump):
        meta = meta_dump(by_alias=True, exclude_none=True)
    if not isinstance(meta, Mapping):
        return {}
    ui = meta.get("ui")
    ui_dump = getattr(ui, "model_dump", None)
    if callable(ui_dump):
        ui = ui_dump(by_alias=True, exclude_none=True)
    if isinstance(ui, Mapping):
        return ui
    # Deprecated flat metadata remains readable for interoperability, while
    # new servers should emit the stable nested ``_meta.ui`` shape.
    flat_uri = meta.get("ui/resourceUri")
    return {"resourceUri": flat_uri} if isinstance(flat_uri, str) else {}


def _tool_visible_to_model(name: str, tool: Any) -> bool:
    """Return whether a tool belongs on the model-facing tool surface."""

    visibility = _tool_ui_metadata(tool).get("visibility")
    if not isinstance(visibility, Sequence) or isinstance(visibility, (str, bytes)):
        return True
    scopes = {str(item) for item in visibility}
    if "model" in scopes:
        return True
    if "model:plan" in scopes:
        # Late, module-qualified reference (not ``from ... import``): breaks the
        # mcp_executor<->tool_ui_metadata import cycle (mcp_executor re-exports
        # this module's names) AND keeps
        # ``monkeypatch.setattr(mcp_executor_module, "_active_session_mode", ...)``
        # (tests/test_tools/test_execution.py) reaching this call.
        from clio_agent.tools import mcp_executor  # noqa: PLC0415

        return mcp_executor._active_session_mode() == "plan"
    return False
