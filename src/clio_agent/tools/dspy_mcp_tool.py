"""Preserve an MCP server's input contract in the agent's DSPy tool schema."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import dspy
from pydantic import PrivateAttr


class MCPBoundTool(dspy.Tool):
    """A bridged tool whose optional backend and CLIO metadata remain optional."""

    _input_contract: dict[str, Any] = PrivateAttr(default_factory=dict)

    def format_as_litellm_function_call(self) -> dict[str, Any]:
        """Return the complete original input schema with owned metadata added."""
        formatted = super().format_as_litellm_function_call()
        formatted["function"]["parameters"] = copy.deepcopy(self._input_contract)
        return formatted


def bind_input_contract(tool: MCPBoundTool, schema: Mapping[str, Any]) -> MCPBoundTool:
    """Preserve required fields and definitions instead of requiring every property."""
    from clio_agent.tools.task_call_context import task_arguments

    contract = copy.deepcopy(dict(schema))
    contract.setdefault("type", "object")
    contract["properties"] = task_arguments(contract.get("properties", {}))
    tool._input_contract = contract
    return tool
