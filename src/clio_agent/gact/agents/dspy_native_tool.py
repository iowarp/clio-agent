"""Lazy-loadable DSPy tool subclass used by CLIO native tools.

Every turn builds its agent's tools afresh, and each tool (and its recording
wrapper) used to re-derive its argument schemas: one pydantic ``TypeAdapter`` and
JSON schema per argument, about 3 ms a tool and 0.2 to 0.35 s of every turn's
start for about 80 native tools (chain runs, 2026-11). The derivation depends only
on the function's code, signature, docstring, the globals its hints resolve in and
the argument descriptions, so it is kept per that key (:func:`_parsed`).
"""

from __future__ import annotations

import copy
import inspect
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, NamedTuple

import dspy
from jsonschema import Draft202012Validator
from referencing import Registry


class _Parsed(NamedTuple):
    name: str
    desc: str
    args: dict[str, Any]
    arg_types: dict[str, Any]
    has_kwargs: bool


#: Parsed schemas by key; cleared whole when it outgrows the bound (keys of
#: defaults that print an address never repeat).
_PARSED: dict[tuple[Any, ...], _Parsed] = {}
_PARSED_LIMIT = 4096
_PARSED_LOCK = threading.Lock()


def _parse_key(func: Callable[..., Any], arg_desc: dict[str, str] | None) -> tuple[Any, ...] | None:
    """Everything ``dspy.Tool._parse_function`` reads from ``func``; ``None`` when the
    callable has no code object to key on (it is then parsed every time)."""
    target: Any = func
    if not (inspect.isfunction(func) or inspect.ismethod(func)):
        target = type(func).__call__
    code = getattr(target, "__code__", None)
    if code is None:
        return None
    return (
        code,
        getattr(func, "__name__", type(func).__name__),
        getattr(func, "__doc__", None),
        getattr(target, "__doc__", None),
        str(inspect.signature(target)),
        id(getattr(target, "__globals__", None)),
        tuple(sorted(arg_desc.items())) if arg_desc else (),
    )


def _parsed(func: Callable[..., Any], arg_desc: dict[str, str] | None) -> _Parsed | None:
    key = _parse_key(func, arg_desc)
    if key is None:
        return None
    with _PARSED_LOCK:
        hit = _PARSED.get(key)
    if hit is None:
        # ``_parse_function`` only reads and sets these attributes.
        shell = SimpleNamespace(name=None, desc=None, args=None, arg_types=None)
        dspy.Tool._parse_function(shell, func, arg_desc)
        hit = _Parsed(shell.name, shell.desc, shell.args, shell.arg_types, shell.has_kwargs)
        with _PARSED_LOCK:
            if len(_PARSED) >= _PARSED_LIMIT:
                _PARSED.clear()
            _PARSED[key] = hit
    return hit


class ToolArgumentError(ValueError):
    """A call's arguments did not fit the tool: the reason and the arguments it takes.

    The model reads this as the call's result, so it is a sentence, not a stack: on X2 a
    ``memory_read(path=...)`` came back as a 1,000-character traceback ending in "Arg
    path is not in the tool's args.", without the name it takes (``name``).
    """

    def __init__(self, tool: str, reason: str, args: dict[str, Any], required: set[str]) -> None:
        listed = ", ".join(
            f"{key} ({(schema or {}).get('type', 'any')}{', required' if key in required else ''})"
            for key, schema in args.items()
        )
        super().__init__(f"{tool}: {reason} Its arguments are: {listed or 'none'}.")


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

    def _parse_function(
        self, func: Callable[..., Any], arg_desc: dict[str, str] | None = None
    ) -> None:
        """``dspy.Tool._parse_function``, derived once per function shape (module doc)."""
        hit = _parsed(func, arg_desc)
        if hit is None:
            super()._parse_function(func, arg_desc)
            return
        # dspy.Tool is a pydantic model whose fields mypy cannot type; declaring them here
        # would re-declare them as pydantic fields (arg_types=None then fails validation).
        self.name = self.name or hit.name  # type: ignore[has-type]
        self.desc = self.desc or hit.desc  # type: ignore[has-type]
        self.args = self.args if self.args is not None else copy.deepcopy(hit.args)  # type: ignore[has-type]
        self.arg_types = (  # type: ignore[has-type]
            self.arg_types if self.arg_types is not None else dict(hit.arg_types)  # type: ignore[has-type]
        )
        self.has_kwargs = hit.has_kwargs

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
            # A required argument (no default) the call left out is a schema error the
            # model can correct, not a TypeError from inside the tool.
            missing = [
                key
                for key, parameter in parameters.items()
                if parameter.default is inspect.Parameter.empty
                and key in (self.args or {})
                and key not in given
            ]
            if missing:
                raise ValueError(f"missing required argument {', '.join(missing)}.")
            return super()._validate_and_parse_args(**given)
        except ValueError as exc:
            from clio_agent.tools import execution  # noqa: PLC0415

            reason = f"invalid arguments: {exc}"
            execution.notify_global_tool_observer(str(self.name), kwargs, "started")
            execution.notify_global_tool_observer(str(self.name), kwargs, "completed", reason)
            required = {
                key
                for key, parameter in parameters.items()
                if parameter.default is inspect.Parameter.empty
            }
            raise ToolArgumentError(
                str(self.name), str(exc), dict(self.args or {}), required
            ) from exc

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
