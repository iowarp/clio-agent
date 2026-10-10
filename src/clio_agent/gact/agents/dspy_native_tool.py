"""Lazy-loadable DSPy tool subclass used by CLIO native tools."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable
from typing import Any

import dspy


class ClioNativeTool(dspy.Tool):
    """DSPy tool whose JSON schema honors declared argument defaults."""

    def __init__(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        desc: str | None = None,
        args: dict[str, Any] | None = None,
        arg_types: dict[str, Any] | None = None,
        arg_desc: dict[str, str] | None = None,
    ) -> None:
        # Strict provider schemas require optional fields to be present. A
        # callable's None default must be expressible as null, both on the
        # wire and in DSPy's argument validator, rather than a guessed zero.
        if args is not None:
            args = copy.deepcopy(args)
            for arg_name, parameter in inspect.signature(func).parameters.items():
                if parameter.default is None and arg_name in args:
                    schema = args[arg_name]
                    args[arg_name] = {"anyOf": [schema, {"type": "null"}]}
                    # An annotation describes the argument, including its null
                    # alternative. Keep it on the argument's public schema.
                    if "description" in schema:
                        args[arg_name]["description"] = schema.pop("description")
        super().__init__(func, name, desc, args, arg_types, arg_desc)

    def format_as_litellm_function_call(self) -> dict[str, Any]:
        """Return a LiteLLM schema requiring only arguments without defaults."""

        formatted = super().format_as_litellm_function_call()
        function_schema = formatted["function"]
        properties = function_schema["parameters"]["properties"]
        signature = inspect.signature(self.func)
        function_schema["parameters"]["required"] = [
            arg_name
            for arg_name, arg_schema in properties.items()
            if "default" not in arg_schema
            and (
                arg_name not in signature.parameters
                or signature.parameters[arg_name].default is inspect.Parameter.empty
            )
        ]
        return formatted
