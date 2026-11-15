"""Which arguments of a tool are paths, and on which machine, as its server declares them.

CLIO cannot tell from an argument's name or value whether ``/data/run.vti`` is a file on
the machine CLIO runs on, a file on the remote machine a server works on, or no file at
all. A server says so in the private ``_meta`` of the tool definition::

    _meta["ai.iowarp.clio/paths"] = {
        "path": {"machine": "remote", "access": "read", "host": "node08"},
        "out": {"machine": "local", "access": "write"},
    }

``machine`` is ``local`` (the machine CLIO runs on), ``remote`` (another machine the server
reaches; ``host`` optionally names it, for display) or ``none`` (path-shaped, but no file,
for example a name inside a dataset). ``access`` is ``read`` (the default) or ``write``.

The contract is honoured where CLIO reasons about a call's files: provenance
(:mod:`clio_agent.gact.artifacts.external_inputs`, ``transform_edges``) and the sandbox
note on a confined server's failed call (:mod:`clio_agent.gact.artifacts.violations`).

The execution boundary binds the called tool's definition for the duration of the call
(:func:`bind_call`), so the readers ask by tool name (:func:`declared_args`) without
reaching for a tool executor. A malformed declaration is ignored with the typed log reason
``path_declaration_invalid``; the call itself is never refused for it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)

#: The private tool-definition ``_meta`` key a server declares its path arguments under.
PATHS_META_KEY = "ai.iowarp.clio/paths"

MACHINE_LOCAL = "local"
MACHINE_REMOTE = "remote"
MACHINE_NONE = "none"
MACHINES = frozenset({MACHINE_LOCAL, MACHINE_REMOTE, MACHINE_NONE})
ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESSES = frozenset({ACCESS_READ, ACCESS_WRITE})

_MAX_HOST_CHARS = 255


@dataclass(frozen=True)
class DeclaredPath:
    """One declared path argument.

    Attributes:
        machine: ``local``, ``remote`` or ``none`` (see the module docstring).
        access: ``read`` or ``write``.
        host: The remote machine's name, for display; empty when not declared.
    """

    machine: str
    access: str = ACCESS_READ
    host: str = ""


_EMPTY: Mapping[str, DeclaredPath] = MappingProxyType({})

#: The tool the running call invokes and its declaration (bound by the execution boundary).
_CALL: ContextVar[tuple[str, Mapping[str, DeclaredPath]] | None] = ContextVar(
    "clio_tool_path_contract_call", default=None
)


def _tool_meta(tool: Any) -> Mapping[str, Any]:
    """The ``_meta`` mapping of a listed tool (FastMCP model or plain mapping)."""

    if tool is None:
        return {}
    meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None)
    if meta is None and isinstance(tool, Mapping):
        meta = tool.get("_meta") or tool.get("meta")
    dump = getattr(meta, "model_dump", None)
    if callable(dump):
        meta = dump(by_alias=True, exclude_none=True)
    return meta if isinstance(meta, Mapping) else {}


def _entry(tool_name: str, arg: Any, raw: Any) -> DeclaredPath | None:
    if not isinstance(arg, str) or not arg or not isinstance(raw, Mapping):
        return _invalid(tool_name, arg, "entry_not_an_object")
    machine = raw.get("machine")
    access = raw.get("access", ACCESS_READ)
    host = raw.get("host", "")
    if machine not in MACHINES:
        return _invalid(tool_name, arg, "machine_unknown")
    if access not in ACCESSES:
        return _invalid(tool_name, arg, "access_unknown")
    if not isinstance(host, str) or len(host) > _MAX_HOST_CHARS or "\n" in host:
        return _invalid(tool_name, arg, "host_invalid")
    return DeclaredPath(machine=machine, access=access, host=host.strip())


def _invalid(tool_name: str, arg: Any, detail: str) -> DeclaredPath | None:
    logger.warning(
        "tool path declaration ignored reason=path_declaration_invalid tool=%s arg=%r detail=%s",
        tool_name,
        arg,
        detail,
    )
    return None


def declared_paths(tool: Any, tool_name: str = "") -> Mapping[str, DeclaredPath]:
    """The path arguments ``tool``'s definition declares (empty when it declares none).

    Args:
        tool: A listed tool definition (a FastMCP ``Tool`` or a plain mapping).
        tool_name: The tool's name, for the log line of a malformed declaration.
    """

    raw = _tool_meta(tool).get(PATHS_META_KEY)
    if raw is None:
        return _EMPTY
    if not isinstance(raw, Mapping):
        _invalid(tool_name, PATHS_META_KEY, "declaration_not_an_object")
        return _EMPTY
    out: dict[str, DeclaredPath] = {}
    for arg, entry in raw.items():
        declared = _entry(tool_name, arg, entry)
        if declared is not None:
            out[arg] = declared
    return MappingProxyType(out) if out else _EMPTY


def bind_call(tool_name: str, tool: Any) -> Any:
    """Bind the running call's tool and its declared path arguments; return ``tool``.

    Called by the execution boundary before a call starts, in the call's own context, so
    the observer's provenance and sandbox readers see the called tool's declaration. A
    later call rebinds it; a reader asking about another tool gets nothing.
    """

    _CALL.set((tool_name, declared_paths(tool, tool_name)))
    return tool


def call_declared_paths(tool_name: str) -> Mapping[str, DeclaredPath]:
    """The bound declaration when ``tool_name`` is the running call's tool, else empty."""

    bound = _CALL.get()
    if bound is None or bound[0] != tool_name:
        return _EMPTY
    return bound[1]


def declared_args(
    tool_name: str, *, machine: str | None = None, access: str | None = None
) -> frozenset[str]:
    """The running call's declared path arguments, filtered by machine and access."""

    return frozenset(
        arg
        for arg, declared in call_declared_paths(tool_name).items()
        if (machine is None or declared.machine == machine)
        and (access is None or declared.access == access)
    )


__all__ = [
    "ACCESS_READ",
    "ACCESS_WRITE",
    "MACHINE_LOCAL",
    "MACHINE_NONE",
    "MACHINE_REMOTE",
    "PATHS_META_KEY",
    "DeclaredPath",
    "bind_call",
    "call_declared_paths",
    "declared_args",
    "declared_paths",
]
