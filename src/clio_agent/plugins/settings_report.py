"""A plugin's settings as a script run reports them: each value its servers read.

A plugin's MCP servers (``mcp.json``) read their configuration from variables:
``${NAME}`` (a key, a token) or ``${NAME:-default}`` (a URL with a default). When a
script run trusts a plugin it cannot ask for any of them, so it says what each one
is: set, empty, or falling back to the plugin's default, and the variable that sets
it. A value from the environment is never reported, only whether it is set; the
default is the plugin's own text from its manifest.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

#: ``${NAME}`` or ``${NAME:-default}``.
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
#: Values CLIO itself supplies to a plugin's servers, never the person's to set.
_SUPPLIED_PREFIXES = ("PLUGIN_", "CLIO_")


def _references(value: Any) -> list[tuple[str, str | None]]:
    if isinstance(value, str):
        return [(match.group(1), match.group(2)) for match in _VARIABLE.finditer(value)]
    if isinstance(value, Mapping):
        return [ref for item in value.values() for ref in _references(item)]
    if isinstance(value, (list, tuple)):
        return [ref for item in value for ref in _references(item)]
    return []


def plugin_settings(
    mcp_servers: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Each variable ``mcp_servers`` reads: ``{name, server, state, default}``.

    ``state`` is ``set`` (the environment has a value), ``default`` (it does not, and
    the plugin declares a default) or ``empty`` (neither: the server gets nothing).
    """

    env = os.environ if environ is None else environ
    rows: dict[str, dict[str, Any]] = {}
    for server, declaration in sorted(mcp_servers.items()):
        spec = declaration.get("spec", declaration) if isinstance(declaration, Mapping) else {}
        for name, default in _references(spec):
            if name.startswith(_SUPPLIED_PREFIXES) or name in rows:
                continue
            state = "set" if env.get(name) else "default" if default else "empty"
            rows[name] = {"name": name, "server": server, "state": state, "default": default}
    return list(rows.values())


def describe(setting: Mapping[str, Any]) -> str:
    """One line: ``EXA_API_KEY empty (set EXA_API_KEY)``."""

    name = setting["name"]
    if setting["state"] == "set":
        return f"{name} set"
    if setting["state"] == "default":
        return f"{name} default {setting['default']} (set {name} to change it)"
    return f"{name} empty (set {name})"


__all__ = ["describe", "plugin_settings"]
