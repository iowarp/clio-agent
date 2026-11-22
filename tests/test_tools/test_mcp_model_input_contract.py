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


def test_an_argument_typed_through_the_contracts_defs_reaches_the_server() -> None:
    """AQ32: ltspice-mcp declares ``circuits`` as
    ``{"items": {"$ref": "#/$defs/ExperimentCircuit"}}``. DSPy checked each argument
    against its property schema alone, the reference pointed at nothing, and every call
    failed before the server. SABOTAGE: drop MCPBoundTool._validate_and_parse_args and
    the call raises "'/$defs/ExperimentCircuit' does not exist"."""
    from types import SimpleNamespace

    from clio_agent.tools.execution import _make_dspy_tool

    seen: list[Any] = []
    schema = {
        "type": "object",
        "properties": {
            "circuits": {"type": "array", "items": {"$ref": "#/$defs/ExperimentCircuit"}}
        },
        "required": ["circuits"],
        "$defs": {
            "ExperimentCircuit": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }
        },
    }
    mcp_tool = SimpleNamespace(description="Run circuits.", inputSchema=schema)
    tool = _make_dspy_tool("run_experiments", mcp_tool, lambda name, args: seen.append(args))
    tool(circuits=[{"path": "rc.cir"}])
    assert seen and seen[0]["circuits"] == [{"path": "rc.cir"}]
    assert tool.args["circuits"] == schema["properties"]["circuits"]  # the prompt is unchanged
    try:
        tool(circuits=[{"wrong": 1}])
    except ValueError as exc:
        assert "path" in str(exc)
    else:  # pragma: no cover - the validation must refuse it
        raise AssertionError("an invalid circuit reached the server")


def test_a_number_sent_as_its_text_reaches_an_mcp_server_as_the_number(tmp_path: Path) -> None:
    """Live 2026-11-17: every researcher's ``literature_search(limit="15")`` was refused
    with "Arg limit is invalid: '15' is not of type 'integer'". A call written as invoke
    markup carries every value as text, and literature and web search are MCP tools, which
    did not decode it (only CLIO's native tools did). SABOTAGE: drop the decode in
    ``MCPBoundTool._validate_and_parse_args`` and this call is refused."""
    server = FastMCP("typed-text-contract")
    seen: list[tuple[int, bool]] = []

    @server.tool
    def search(query: str, limit: int = 10, exact: bool = False) -> dict[str, Any]:
        """Search with a result limit."""
        seen.append((limit, exact))
        return {"query": query, "limit": limit}

    executor = create_sync_tool_executor(server)
    try:
        tool = next(t for t in executor.to_dspy_tools() if t.name == "search")
        result = json.loads(tool(query="microplastics", limit="15", exact="true"))
        assert result["limit"] == 15
        assert seen == [(15, True)]
        for wrong in ("1.5", "ten"):
            try:
                tool(query="microplastics", limit=wrong)
            except Exception:  # noqa: BLE001 - the refusal type is DSPy's own
                continue
            raise AssertionError(f"limit={wrong!r} was taken")
    finally:
        executor.close()
