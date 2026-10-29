"""Tools a plugin contributes from ``ai.iowarp.clio/tools/*.py``.

Two forms are collected from each module:

* :func:`tool` turns a Python function into a tool the agent can call::

      @tool(read_only=True)
      def plato_optimize(mesh: str, target: float) -> dict: ...

* :func:`native_tool` claims a tool CLIO already provides by name, so the
  plugin that owns the capability is the one that brings it::

      shell_bash = native_tool("shell_bash")

Tool names are global: two resolved plugins exposing the same name is a
resolution error naming both.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """A Python function exposed as a tool."""

    name: str
    function: Callable[..., Any]
    description: str = ""
    read_only: bool = False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.function(*args, **kwargs)


@dataclass(frozen=True)
class NativeToolRef:
    """A CLIO built-in tool claimed by the plugin that owns the capability."""

    name: str


def tool(
    function: Callable[..., Any] | None = None,
    *,
    name: str = "",
    description: str = "",
    read_only: bool = False,
) -> Any:
    """Declare a Python tool. Usable bare (``@tool``) or with options."""

    def wrap(fn: Callable[..., Any]) -> ToolSpec:
        return ToolSpec(
            name=name or fn.__name__,
            function=fn,
            description=description or (fn.__doc__ or "").strip(),
            read_only=read_only,
        )

    if function is not None:
        return wrap(function)
    return wrap


def native_tool(name: str) -> NativeToolRef:
    """Claim the CLIO built-in tool ``name`` for this plugin."""

    return NativeToolRef(name=name)
