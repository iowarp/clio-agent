"""Per-server host semantics a plugin declares (HS1 to HS5).

A plugin's ``ai.iowarp.clio`` extension declares ``catalogs``, ``tabs``,
``accounts`` and ``elicitation`` (see ``docs/design/host-semantics-contract.md``).
CLIO applies them per MCP server: :func:`server_host` is the slice one server of
the plugin gets, carried on the resolution's ``mcp_servers`` row as ``host``.

The gateway builds a server's client without a session, so the slices of every
registered server set are also kept here by plugin id (:func:`remember`,
:func:`plugin_host`). Nothing here names a particular plugin or server.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.plugins.records import PluginRecord

_LOCK = threading.Lock()
#: ``plugin id -> {server name -> host slice}`` of every registered server set.
_BY_PLUGIN: dict[str, dict[str, dict[str, Any]]] = {}

#: ``spec.source`` of a server a plugin declares in its ``mcp.json``.
PLUGIN_SOURCE_PREFIX = "plugin:"


def server_host(record: PluginRecord, server: str) -> dict[str, Any]:
    """The host semantics ``record`` declares for its server ``server`` ({} for none)."""

    host = record.host or {}
    out: dict[str, Any] = {}
    catalogs = [catalog.id for catalog in record.catalogs if catalog.id]
    if catalogs:
        out["catalogs"] = list(dict.fromkeys(catalogs))
    tabs = [dict(tab) for tab in host.get("tabs") or [] if tab.get("server") == server]
    if tabs:
        out["tabs"] = tabs
    if host.get("accounts"):
        out["accounts"] = [dict(row) for row in host["accounts"]]
    if host.get("elicitation"):
        out["elicitation"] = dict(host["elicitation"])
    return out


def remember(mcp_servers: Mapping[str, Mapping[str, Any]]) -> None:
    """Keep the host slices of a server set's rows, by plugin id."""

    with _LOCK:
        for name, row in mcp_servers.items():
            plugin = str(row.get("plugin") or "")
            if plugin:
                _BY_PLUGIN.setdefault(plugin, {})[str(name)] = dict(row.get("host") or {})


def plugin_host(plugin: str, server: str) -> dict[str, Any]:
    """The remembered host slice of ``plugin``'s server ``server`` ({} when unknown)."""

    with _LOCK:
        return dict((_BY_PLUGIN.get(plugin) or {}).get(server) or {})


def spec_plugin(source: str) -> str:
    """The plugin id of a spec ``source`` (``plugin:<id>``), else ""."""

    return source[len(PLUGIN_SOURCE_PREFIX) :] if source.startswith(PLUGIN_SOURCE_PREFIX) else ""


def spec_a2ui_catalogs(spec: Any) -> tuple[str, ...]:
    """The A2UI catalog ids of the plugin declaring server ``spec`` (HS1a); () otherwise."""

    plugin = spec_plugin(str(getattr(spec, "source", "") or ""))
    if not plugin:
        return ()
    return tuple(plugin_host(plugin, str(getattr(spec, "name", "") or "")).get("catalogs") or ())


def account_variable(provider: str) -> str:
    """``CLIO_ACCOUNT_<PROVIDER>_FILE``: the variable naming a provider's grant file."""

    return f"CLIO_ACCOUNT_{provider.upper()}_FILE"


def declared_accounts() -> dict[str, dict[str, list[str]]]:
    """``provider -> {plugin id -> scopes}`` over every registered plugin server (HS4)."""

    out: dict[str, dict[str, list[str]]] = {}
    with _LOCK:
        slices = [(plugin, dict(servers)) for plugin, servers in _BY_PLUGIN.items()]
    for plugin, servers in slices:
        for host in servers.values():
            for row in host.get("accounts") or ():
                provider = str(row.get("provider") or "")
                if not provider:
                    continue
                scopes = out.setdefault(provider, {}).setdefault(plugin, [])
                for scope in row.get("scopes") or ():
                    if str(scope) not in scopes:
                        scopes.append(str(scope))
    return out


def declared_account_scopes(provider: str) -> list[str]:
    """Every scope a registered plugin declares for ``provider``, in first-seen order."""

    scopes: list[str] = []
    for plugin_scopes in declared_accounts().get(provider, {}).values():
        scopes.extend(scope for scope in plugin_scopes if scope not in scopes)
    return scopes


__all__ = [
    "PLUGIN_SOURCE_PREFIX",
    "account_variable",
    "declared_account_scopes",
    "declared_accounts",
    "plugin_host",
    "remember",
    "server_host",
    "spec_a2ui_catalogs",
    "spec_plugin",
]
