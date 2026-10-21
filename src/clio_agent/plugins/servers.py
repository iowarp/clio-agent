"""Plugin MCP servers as gateway server sets.

A resolved agent's plugins declare MCP servers in ``mcp.json`` (Agent Plugins
1.0.0). The workspace fleet mounts them as one *server set*: a deterministic
key over the declarations, so every session with the same servers shares the
mounted namespaces and a second set merges into the resident fleet.

Placeholders follow the specification: ``${PLUGIN_ROOT}`` and ``${PLUGIN_DATA}``
are expanded in ``args``, ``env`` and ``cwd``, and both are set in the server's
environment. CLIO starts stdio servers in the session workspace (its write
confinement boundary) rather than the plugin root; ``PLUGIN_ROOT`` still names
the plugin's own files.

CLIO also supplies values a server can only get from the running client:
``${CLIO_PROVENANCE_CONFIG}`` is a CLIO YAML file with the effective
provenance configuration, for servers that query the stores CLIO writes. It is
materialized only when a declaration references it; an unsupplied reference is
left in place so the generic ``${VAR}`` expansion reports it as unset.
"""

from __future__ import annotations

import hashlib
import json
import threading
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROVENANCE_CONFIG_VAR = "CLIO_PROVENANCE_CONFIG"
CLIO_SUPPLIED_VARIABLES = (PROVENANCE_CONFIG_VAR,)
_SUPPLIED = re.compile(r"\$\{(" + "|".join(CLIO_SUPPLIED_VARIABLES) + r")(?::-[^}]*)?\}")

_LOCK = threading.Lock()
_SETS: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}


def plugin_data_dir(plugin_id: str) -> Path:
    """The persistent ``PLUGIN_DATA`` folder of one installed plugin."""

    from clio_agent.plugins.service import plugin_store_root  # noqa: PLC0415

    path = plugin_store_root() / "data" / plugin_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def references_variable(value: Any, name: str) -> bool:
    """True when ``value`` (any nesting of str/list/mapping) references ``${name}``."""

    if isinstance(value, str):
        return any(match.group(1) == name for match in _SUPPLIED.finditer(value))
    if isinstance(value, Mapping):
        return any(references_variable(item, name) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(references_variable(item, name) for item in value)
    return False


def _expand(value: str, root: str, data: str, supplied: Mapping[str, str]) -> str:
    value = value.replace("${PLUGIN_ROOT}", root).replace("${PLUGIN_DATA}", data)
    return _SUPPLIED.sub(lambda match: supplied.get(match.group(1), match.group(0)), value)


def server_declarations(
    mcp_servers: Mapping[str, Mapping[str, Any]],
    supplied: Mapping[str, str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Gateway declarations for a resolution's ``mcp_servers``, grouped by plugin.

    ``supplied`` carries the CLIO-supplied values the declarations reference.
    """

    values = dict(supplied or {})
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, row in sorted(mcp_servers.items()):
        spec = dict(row["spec"])
        root = str(row["root"])
        data = str(plugin_data_dir(str(row["plugin"])))
        if spec.get("type") == "stdio":
            env = {
                key: _expand(str(value), root, data, values)
                for key, value in (spec.get("env") or {}).items()
            }
            env.update({"PLUGIN_ROOT": root, "PLUGIN_DATA": data})
            env.update({key: value for key, value in values.items() if references_variable(spec, key)})
            out.setdefault(str(row["plugin"]), {})[name] = {
                "command": str(spec["command"]).replace("./", root.rstrip("/") + "/", 1)
                if str(spec["command"]).startswith("./")
                else str(spec["command"]),
                "args": [_expand(str(arg), root, data, values) for arg in spec.get("args") or []],
                "env": env,
            }
        else:
            out.setdefault(str(row["plugin"]), {})[name] = {
                "type": "http",
                "url": spec["url"],
                "headers": dict(spec.get("headers") or {}),
            }
    return out


def register_server_set(
    mcp_servers: Mapping[str, Mapping[str, Any]], supplied: Mapping[str, str] | None = None
) -> str:
    """Register a resolution's servers; return the set key ("" for none)."""

    if not mcp_servers:
        return ""
    declarations = server_declarations(mcp_servers, supplied)
    blob = json.dumps(declarations, sort_keys=True, separators=(",", ":"))
    key = "servers-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    with _LOCK:
        _SETS[key] = declarations
    return key


def server_set_declarations(key: str) -> dict[str, dict[str, dict[str, Any]]]:
    """The ``{plugin: {server: declaration}}`` registered under ``key`` ({} for none)."""

    if not key:
        return {}
    with _LOCK:
        declarations = _SETS.get(key)
    if declarations is None:
        raise KeyError(f"MCP server set {key!r} is not registered in this process")
    return dict(declarations)
