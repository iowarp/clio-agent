"""Tools a plugin contributes from ``ai.iowarp.clio/tools/*.py``.

Two forms are collected from each module:

* :func:`tool` turns a Python function into a tool the agent can call::

      @tool(read_only=True)
      def plato_optimize(mesh: str, target: float) -> dict: ...

* :func:`native_tool` claims a tool CLIO already provides by name, so the
  plugin that owns the capability is the one that brings it::

      shell_bash = native_tool("shell_bash")

Both forms take ``callers`` (``"direct"``, ``"code"`` or ``"both"``: who may call
the tool; ``None`` keeps the tool's own default, ``both`` unless it says otherwise)
and ``permission`` (``"allow"``, ``"ask"`` or ``"deny"``: the default permission,
below the person's rules and above the approval mode; ``None`` follows the tool's
annotations). An unknown value is an error when the plugin loads.

Tool names are global: two resolved plugins exposing the same name is a
resolution error naming both.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from clio_agent.tools.tool_settings import check_callers, check_permission


@dataclass(frozen=True)
class ToolSpec:
    """A Python function exposed as a tool."""

    name: str
    function: Callable[..., Any]
    description: str = ""
    read_only: bool = False
    #: True: offered by name and loaded with ``search_tools``; None: the plugin decides.
    defer: bool | None = None
    #: Who may call it: ``direct``, ``code`` or ``both``; None: the default (``both``).
    callers: str | None = None
    #: Default permission: ``allow``, ``ask`` or ``deny``; None: from ``read_only``.
    permission: str | None = None

    def __post_init__(self) -> None:
        check_callers(self.callers, f"tool {self.name!r}")
        check_permission(self.permission, f"tool {self.name!r}")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.function(*args, **kwargs)


@dataclass(frozen=True)
class NativeToolRef:
    """A CLIO built-in tool claimed by the plugin that owns the capability."""

    name: str
    #: True: offered by name and loaded with ``search_tools``; None: the plugin decides.
    defer: bool | None = None
    #: Who may call it; None keeps the built-in's own declaration.
    callers: str | None = None
    #: Default permission; None follows the built-in's annotations.
    permission: str | None = None

    def __post_init__(self) -> None:
        check_callers(self.callers, f"native_tool {self.name!r}")
        check_permission(self.permission, f"native_tool {self.name!r}")


def tool(
    function: Callable[..., Any] | None = None,
    *,
    name: str = "",
    description: str = "",
    read_only: bool = False,
    defer: bool | None = None,
    callers: str | None = None,
    permission: str | None = None,
) -> Any:
    """Declare a Python tool. Usable bare (``@tool``) or with options.

    ``defer=True`` offers it by name only until ``search_tools`` loads its schema.
    ``callers`` and ``permission`` are the plugin's defaults (see the module docstring).
    """

    def wrap(fn: Callable[..., Any]) -> ToolSpec:
        return ToolSpec(
            name=name or fn.__name__,
            function=fn,
            description=description or (fn.__doc__ or "").strip(),
            read_only=read_only,
            defer=defer,
            callers=callers,
            permission=permission,
        )

    if function is not None:
        return wrap(function)
    return wrap


def native_tool(
    name: str,
    *,
    defer: bool | None = None,
    callers: str | None = None,
    permission: str | None = None,
) -> NativeToolRef:
    """Claim the CLIO built-in tool ``name`` for this plugin.

    ``defer=True`` offers it by name only until ``search_tools`` loads its schema.
    ``callers`` and ``permission`` are the plugin's defaults (see the module docstring).
    """

    return NativeToolRef(name=name, defer=defer, callers=callers, permission=permission)
