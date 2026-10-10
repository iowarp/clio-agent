"""Real MCP dispatch must preserve the argument schema shown to the model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.execution import create_sync_tool_executor


def test_model_contract_keeps_description_optional_and_forwards_only_backend_arguments(
    tmp_path: Path,
) -> None:
    server = FastMCP("owned-input-contract")
    target = tmp_path / "sentinel.txt"
    target.write_text("independent action completed", encoding="utf-8")
    seen: list[tuple[str, str]] = []

    @server.tool
    def read_owned(filepath: str, mode: str = "head") -> dict[str, Any]:
        """Read the real owned input with an optional output mode."""
        seen.append((filepath, mode))
        return {"content": Path(filepath).read_text(encoding="utf-8"), "mode": mode}

    executor = create_sync_tool_executor(server)
    try:
        tool = next(t for t in executor.to_dspy_tools() if t.name == "read_owned")
        schema = tool.format_as_litellm_function_call()["function"]["parameters"]
        assert schema["required"] == ["filepath"]
        assert schema["properties"]["mode"]["default"] == "head"
        assert "_clio_task_description" in schema["properties"]
        result = json.loads(
            tool(filepath=str(target), _clio_task_description="Read the owned sentinel")
        )
        assert result["content"] == "independent action completed"
        assert result["mode"] == "head"
        assert seen == [(str(target), "head")]
    finally:
        executor.close()
