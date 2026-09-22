"""Lazy-loadable DSPy tool subclass used by CLIO native tools."""

from __future__ import annotations

from typing import Any

import dspy


class ClioNativeTool(dspy.Tool):
    """DSPy tool whose JSON schema honors declared argument defaults."""

    def format_as_litellm_function_call(self) -> dict[str, Any]:
        """Return a LiteLLM schema requiring only arguments without defaults."""

        formatted = super().format_as_litellm_function_call()
        function_schema = formatted["function"]
        properties = function_schema["parameters"]["properties"]
        function_schema["parameters"]["required"] = [
            arg_name for arg_name, arg_schema in properties.items() if "default" not in arg_schema
        ]
        return formatted
