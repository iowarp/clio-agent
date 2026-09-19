"""Schema + domain projection for GET /v1/catalog/tools (#1350).

The desktop Tools view wants typed inputs/outputs and a server-declared
``domain`` to group by, instead of guessing either from the tool NAME via a
client-side regex. This owner module derives both, generically, from facts
the tool already carries:

* ``input_schema`` — a constructed dspy/``ClioNativeTool``'s own
  ``format_as_litellm_function_call()`` (the default-aware required-list DSPy
  schema every native tool already declares), or the in-process gateway's own
  MCP ``input_schema`` for the four static fs/shell rows that have no
  directly-constructed tool object — read via the synchronous, no-I/O
  :func:`clio_agent.tools.gateway.list_builtin_tool_definitions` (memoized at
  module scope, see :func:`_static_gateway_rows`), never the fully async
  gateway-client listing;
* ``output_schema`` — ``pydantic.TypeAdapter(<return annotation>).json_schema()``
  off the tool's own callable. No per-tool special-casing: a tool that returns
  a bare ``str`` already projects to ``{"type": "string"}`` this way, a
  ``dict[str, Any]`` to a permissive object schema. A callable with NO return
  annotation, an UNRESOLVABLE forward-ref annotation, or an annotation
  pydantic cannot build a schema for is a genuine gap in the tool —
  :class:`ToolSchemaError` names it rather than guessing a shape for it;
* ``domain`` — read back off the tool's own construction-time declaration
  (:func:`clio_agent.gact.agents.tool_instrumentation.tool_domain` for native
  tools; the static :mod:`clio_agent.tools.catalog` entry for the four
  gateway rows).

:func:`builtin_tool_rows` reuses
:func:`clio_agent.gact.catalog._builtin_tool_declarations` — the SAME
declaration pass :func:`clio_agent.gact.catalog._builtin_tools` (every other
consumer) uses — so this route's rows can never drift on name/title/
description from the plain sync listing.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import TypeAdapter
from pydantic.errors import PydanticSchemaGenerationError

from clio_agent.gact.agents.tool_instrumentation import tool_domain
from clio_agent.gact.catalog import (
    _builtin_tool_declarations,
    _tool_domain_for_catalog,
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
)
from clio_agent.gact.types import Tool, ToolDomain

#: Permissive fallback for a tool declaring no arguments (or whose gateway row
#: carries no schema) — a genuine ``{}`` input is still "an object with no
#: required properties", never an omitted key.
_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class ToolSchemaError(ValueError):
    """A builtin tool is missing a fact its schema needs (never guessed)."""


def _gateway_tool_funcs() -> dict[str, Callable[..., Any]]:
    """Return the 4 static gateway (fs/shell) tools' underlying python callables.

    These have no directly-constructed ``dspy.Tool`` in
    :func:`clio_agent.gact.catalog._builtin_tool_declarations` (they run
    through the in-process MCP gateway) — this mapping exists ONLY so
    :func:`tool_output_schema` can still read their return annotation. Their
    INPUT schema instead comes from the gateway's own MCP introspection
    (:func:`clio_agent.tools.gateway.list_gateway_tools`), the richer,
    already-declared JSON Schema.
    """

    from clio_agent.tools.servers.fs_server import (  # noqa: PLC0415
        apply_edit_write,
        propose_edit,
        read_file,
    )
    from clio_agent.tools.servers.shell_server import bash  # noqa: PLC0415

    return {
        "fs_read_file": read_file,
        "fs_propose_edit": propose_edit,
        "fs_apply_edit_write": apply_edit_write,
        "shell_bash": bash,
    }


def tool_input_schema(
    tool_obj: Any, *, name: str, gateway_row: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return one builtin tool's model-facing JSON input schema.

    A constructed dspy/``ClioNativeTool`` declares its own LiteLLM
    function-call schema — the single source, rather than re-deriving
    ``args`` by hand, so it honors ``ClioNativeTool``'s default-aware
    ``required`` list. The four static gateway (fs/shell) rows carry no
    constructed tool object; their schema is the gateway's own already-
    authoritative MCP ``input_schema`` (``gateway_row``).
    """

    format_call = getattr(tool_obj, "format_as_litellm_function_call", None)
    if callable(format_call):
        parameters = format_call()["function"]["parameters"]
        if isinstance(parameters, Mapping):
            return dict(parameters)
    if gateway_row is not None:
        schema = gateway_row.get("input_schema")
        if isinstance(schema, Mapping) and schema:
            return dict(schema)
    return dict(_EMPTY_OBJECT_SCHEMA)


def tool_output_schema(func: Callable[..., Any] | None, *, name: str) -> dict[str, Any]:
    """Return one builtin tool's output JSON schema from its callable's return annotation.

    Derived generically via ``pydantic.TypeAdapter`` off ``typing.get_type_hints``
    (not ``inspect.signature().return_annotation`` directly — several tool
    modules use ``from __future__ import annotations``, which stores
    annotations as unevaluated strings) — no per-tool special-casing. A
    MISSING return annotation raises :class:`ToolSchemaError`
    (``return_annotation_missing:<name>``) rather than guessing a shape; the
    fix is to annotate the tool, not to special-case it here.

    Two further failure modes are typed rather than left to escape as a raw
    exception into the route: a ``TYPE_CHECKING``-only forward-ref annotation
    ``typing.get_type_hints`` cannot resolve (``NameError``), and an
    annotation pydantic genuinely cannot build a schema for — e.g.
    ``Awaitable[...]`` or a DSPy ``Prediction`` — (``PydanticSchemaGenerationError``).
    Both surface as :class:`ToolSchemaError`
    (``return_annotation_unresolvable:<name>:<ExceptionType>``); the fix is
    the same as a missing annotation — annotate the tool with a real,
    resolvable, schema-representable type.
    """

    if func is None:
        raise ToolSchemaError(f"return_annotation_missing:{name}")
    try:
        hints = typing.get_type_hints(func)
    except NameError as exc:
        raise ToolSchemaError(
            f"return_annotation_unresolvable:{name}:{type(exc).__name__}"
        ) from exc
    if "return" not in hints:
        raise ToolSchemaError(f"return_annotation_missing:{name}")
    try:
        return TypeAdapter(hints["return"]).json_schema()
    except PydanticSchemaGenerationError as exc:
        raise ToolSchemaError(
            f"return_annotation_unresolvable:{name}:{type(exc).__name__}"
        ) from exc


#: Memoized ``{name: {"description": ..., "input_schema": ...}}`` for the 4 static fs/shell
#: rows. ``None`` until first computed. See :func:`_static_gateway_rows`.
_STATIC_GATEWAY_ROWS: dict[str, dict[str, Any]] | None = None


def _static_gateway_rows() -> dict[str, dict[str, Any]]:
    """Return the 4 static fs/shell rows' description + input schema, computed ONCE.

    ``GET /v1/catalog/tools`` used to call the fully async
    :func:`clio_agent.tools.gateway.list_gateway_tools` on every request — a fresh event loop, an
    in-memory FastMCP client per namespace, and psutil walks, off the request's own loop (the
    ~20ms -> ~109ms regression #1350 review caught). The four fs/shell rows never change at
    runtime, so instead this reads the synchronous, no-I/O
    :func:`clio_agent.tools.gateway.list_builtin_tool_definitions` ONCE and caches the small
    description/input-schema projection at module scope for every subsequent request.
    """

    global _STATIC_GATEWAY_ROWS
    if _STATIC_GATEWAY_ROWS is None:
        from clio_agent.tools.gateway import list_builtin_tool_definitions  # noqa: PLC0415

        _STATIC_GATEWAY_ROWS = {
            name: {
                "description": getattr(mcp_tool, "description", "") or "",
                "input_schema": getattr(mcp_tool, "input_schema", None),
            }
            for name, mcp_tool in list_builtin_tool_definitions().items()
        }
    return _STATIC_GATEWAY_ROWS


async def builtin_tool_rows() -> list[Tool]:
    """Return the ``GET /v1/catalog/tools`` rows: builtin tools with schemas + domain."""

    gateway_rows = _static_gateway_rows()
    gateway_funcs = _gateway_tool_funcs()

    rows: list[Tool] = []
    for name, title, description, tool_obj in _builtin_tool_declarations():
        gateway_row = gateway_rows.get(name)
        if tool_obj is None and gateway_row is None:
            # A static gateway (fs/shell) name with no matching gateway listing is a genuine
            # gap -- the fs/shell server's tool list drifted from what this catalog expects.
            # Never silently serve a blank/empty schema for it.
            raise ToolSchemaError(f"gateway_listing_missing:{name}")
        func = getattr(tool_obj, "func", None) if tool_obj is not None else gateway_funcs.get(name)
        row_description = description or (gateway_row or {}).get("description") or ""
        domain: ToolDomain | None = (
            tool_domain(tool_obj) if tool_obj is not None else _tool_domain_for_catalog(name)
        )
        rows.append(
            Tool(
                id=name,
                source="builtin",
                name=name,
                title=title or name.replace("_", " ").title(),
                description=row_description,
                owner=_tool_owner_for_catalog(name),
                tags=_tool_tags_for_catalog(name),
                visible_to=_tool_visible_to_for_catalog(name),
                input_schema=tool_input_schema(tool_obj, name=name, gateway_row=gateway_row),
                output_schema=tool_output_schema(func, name=name),
                domain=domain,
            )
        )
    return rows
