"""Shared machinery for the synthetic MCP servers.

A server is a list of :class:`ToolSpec` rows and a responder. Each spec becomes a
FastMCP tool with a real typed signature (so its schema looks like a hand
written tool), every call is appended to a JSONL call log, and the responder
returns a plausible result from seeded, read-only state: writes report what
they would change but never persist, so repeated runs see the same world.

This module runs standalone inside a plugin folder, so it imports only the
standard library, pydantic and FastMCP.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import Field

REQUIRED: Any = inspect.Parameter.empty
_TYPES: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}


@dataclass(frozen=True)
class Param:
    """One tool argument. ``kind`` is a type name, ``enum:a|b``, optionally ending ``?``."""

    name: str
    kind: str
    description: str
    default: Any = REQUIRED


@dataclass(frozen=True)
class ToolSpec:
    """One tool. ``effect`` is ``list``, ``read`` or ``write``."""

    name: str
    effect: str
    description: str
    params: tuple[Param, ...] = ()


def p(name: str, kind: str, description: str, default: Any = REQUIRED) -> Param:
    """Shorthand for :class:`Param`; an optional kind (``int?``) defaults to ``None``."""
    if kind.endswith("?") and default is REQUIRED:
        default = None
    return Param(name, kind, description, default)


def t(name: str, effect: str, description: str, *params: Param) -> ToolSpec:
    """Shorthand for :class:`ToolSpec`."""
    return ToolSpec(name, effect, description, params)


def annotation(param: Param) -> Any:
    """The typed annotation FastMCP turns into the argument's JSON schema."""
    base = param.kind.rstrip("?")
    if base.startswith("enum:"):
        kind: Any = Literal[tuple(base[5:].split("|"))]
    else:
        kind = _TYPES[base]
    if param.kind.endswith("?"):
        kind = kind | None
    return Annotated[kind, Field(description=param.description)]


def call_log_path(env: Mapping[str, str] | None = None) -> Path | None:
    """Where calls are logged: ``EVAL_CALL_LOG``, else ``$PLUGIN_DATA/calls.jsonl``.

    An unexpanded ``${...}`` value (a host that does not expand variables in
    ``env``) counts as unset.
    """
    source = os.environ if env is None else env
    raw = str(source.get("EVAL_CALL_LOG", "")).strip()
    if raw and not raw.startswith("${"):
        return Path(raw).expanduser()
    data = str(source.get("PLUGIN_DATA", "")).strip()
    return Path(data) / "calls.jsonl" if data else None


class CallLog:
    """Append-only JSONL record of every tool call a server receives."""

    def __init__(self, server: str, path: Path | None) -> None:
        self.server = server
        self.path = path
        self._lock = threading.Lock()

    def record(self, tool: str, args: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        row = {
            "ts": time.time(),
            "server": self.server,
            "tool": tool,
            "args": dict(args),
            "pid": os.getpid(),
        }
        line = json.dumps(row, sort_keys=True, default=str) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)


Responder = Callable[[ToolSpec, dict[str, Any]], Any]


def _handler(spec: ToolSpec, respond: Responder, log: CallLog) -> Callable[..., Any]:
    def handler(**kwargs: Any) -> Any:
        log.record(spec.name, kwargs)
        return respond(spec, kwargs)

    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                param.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=param.default,
                annotation=annotation(param),
            )
            for param in spec.params
        ]
    )
    handler.__annotations__ = {param.name: annotation(param) for param in spec.params}
    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    return handler


def build_server(
    name: str, instructions: str, specs: list[ToolSpec], respond: Responder, log: CallLog
) -> FastMCP:
    """A FastMCP server exposing ``specs``."""
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError(f"{name}: duplicate tool names")
    server = FastMCP(name, instructions=instructions)
    for spec in specs:
        server.add_tool(
            Tool.from_function(
                _handler(spec, respond, log),
                name=spec.name,
                description=spec.description,
                annotations=ToolAnnotations(readOnlyHint=spec.effect != "write"),
            )
        )
    return server


def _id_param(spec: ToolSpec) -> str:
    required = [
        q.name
        for q in spec.params
        if q.default is REQUIRED and (q.name.endswith("_id") or q.name == "sku")
    ]
    return required[0] if required else ""


class CollectionResponder:
    """Answers from named collections of seeded records, keyed by tool-name prefix.

    ``collections`` maps a prefix (``light``, ``binary_sensor``) to
    ``{record id: attributes}``; the longest prefix of a tool's name picks its
    collection. ``extras`` answers named tools directly.
    """

    def __init__(
        self,
        collections: Mapping[str, Mapping[str, Mapping[str, Any]]],
        extras: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
    ) -> None:
        self.collections = collections
        self.extras = dict(extras or {})
        self._prefixes = sorted(collections, key=len, reverse=True)

    def collection(self, tool: str) -> str:
        return next((x for x in self._prefixes if tool == x or tool.startswith(f"{x}_")), "")

    def __call__(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        if spec.name in self.extras:
            return self.extras[spec.name](args)
        key = self.collection(spec.name)
        rows = self.collections.get(key, {})
        if spec.effect == "list":
            area = args.get("area")
            items = [
                {"id": rid, **attrs}
                for rid, attrs in rows.items()
                if not area or attrs.get("area") == area
            ]
            return {"count": len(items), "items": items}
        id_name = _id_param(spec)
        record_id = str(args.get(id_name) or "") if id_name else ""
        if id_name and record_id not in rows:
            return {
                "ok": False,
                "error": f"unknown {key or 'record'} id {record_id!r}",
                "known_ids": sorted(rows)[:12],
            }
        current = dict(rows.get(record_id, {}))
        if spec.effect == "read":
            return {"id": record_id, **current} if record_id else {"items": list(rows)}
        changes = {k: v for k, v in args.items() if k != id_name and v is not None}
        return {
            "ok": True,
            "id": record_id,
            "action": spec.name,
            "applied": changes,
            "state": {**current, **changes},
        }
