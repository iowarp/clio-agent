"""Lazy-loadable DSPy tool subclass used by CLIO native tools."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable
from typing import Any

import dspy
from jsonschema import Draft202012Validator
from referencing import Registry


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
                    if Draft202012Validator(schema, registry=Registry()).is_valid(None):
                        continue
                    args[arg_name] = {"anyOf": [schema, {"type": "null"}]}
                    # An annotation describes the argument, including its null
                    # alternative. Keep it on the argument's public schema.
                    for annotation in ("description", "default"):
                        if annotation in schema:
                            args[arg_name][annotation] = schema.pop(annotation)
        super().__init__(func, name, desc, args, arg_types, arg_desc)

    def _validate_and_parse_args(self, **kwargs: Any) -> Any:
        """The call's arguments, validated; a failed call is recorded with its reason.

        JSON null for an argument that has a default means it was not given: the
        argument is left out and its default applies. Any other invalid argument
        never reaches the tool, so the call is recorded here as a failed call, as
        the transcript and the UI show every call the agent made.
        """

        parameters = inspect.signature(self.func).parameters
        given = {
            key: value
            for key, value in kwargs.items()
            if not (
                value is None
                and key in parameters
                and parameters[key].default not in (None, inspect.Parameter.empty)
            )
        }
        try:
            return super()._validate_and_parse_args(**given)
        except ValueError as exc:
            from clio_agent.tools import execution  # noqa: PLC0415

            reason = f"invalid arguments: {exc}"
            execution.notify_global_tool_observer(str(self.name), kwargs, "started")
            execution.notify_global_tool_observer(str(self.name), kwargs, "completed", reason)
            raise

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
