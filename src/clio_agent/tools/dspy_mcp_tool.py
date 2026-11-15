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

    def _validate_and_parse_args(self, **kwargs: Any) -> Any:
        """DSPy's per-argument check, with the contract's definitions in reach.

        DSPy validates each argument against its own property schema, which is then
        the root, so ``{"$ref": "#/$defs/ExperimentCircuit"}`` pointed at nothing and
        every call of a tool with ``$defs`` failed before reaching its server (AQ32).
        """
        defs = {
            key: self._input_contract[key]
            for key in ("$defs", "definitions")
            if key in self._input_contract
        }
        if not defs:
            return super()._validate_and_parse_args(**kwargs)
        args = {
            name: {**schema, **defs} if isinstance(schema, Mapping) else schema
            for name, schema in (self.args or {}).items()
        }
        return dspy.Tool._validate_and_parse_args(self.model_copy(update={"args": args}), **kwargs)


def bind_input_contract(tool: MCPBoundTool, schema: Mapping[str, Any]) -> MCPBoundTool:
    """Preserve required fields and definitions instead of requiring every property."""
    from clio_agent.tools.task_call_context import task_arguments

    contract = copy.deepcopy(dict(schema))
    contract.setdefault("type", "object")
    contract["properties"] = task_arguments(contract.get("properties", {}))
    tool._input_contract = contract
    return tool
