"""An enabled connector as the person's saved MCP connection (CN1, CN2, CN8).

The connection has the connector's id as its name, ``connector: <id>`` as its
marker and ``connection_only: true``: it is never mounted on its own, only through
the plugin that follows the connector (:mod:`.follow`), so its scope is one rule.
Address and value needs are written into the declaration; a secret stays a
``${need.<key>}`` placeholder there and is filled only when the server starts
(:func:`fill_secrets`, called from ``tools.mcp_config.spec_from_declaration``).
A sign-in need adds CLIO's managed OAuth block; the sign-in itself is the existing
``/v1/mcp/configuration/{id}/sign-in`` flow, whose tokens are keyed by this
connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.connectors.entries import PLACEHOLDER, Entry

CONNECTOR_KEY = "connector"


def need_value(entry: Entry, values: Mapping[str, str], key: str) -> str:
    """The value written for ``key``: the person's, else the need's default, else empty."""

    need = entry.need(key)
    if need is None or need.kind in {"secret", "sign-in"}:
        return ""
    value = values.get(key)
    if value not in (None, ""):
        return str(value)
    return need.default or ""


def _substitute(entry: Entry, values: Mapping[str, str], text: str, *, keep_secrets: bool) -> str:
    def one(match: Any) -> str:
        need = entry.need(match.group(1))
        if need is not None and need.kind == "secret" and keep_secrets:
            return match.group(0)
        return need_value(entry, values, match.group(1))

    return PLACEHOLDER.sub(one, text)


def declaration(entry: Entry, values: Mapping[str, str]) -> dict[str, Any]:
    """The saved-connection declaration for ``entry`` with the person's ``values``."""

    server = entry.server
    sub = lambda text: _substitute(entry, values, str(text), keep_secrets=True)  # noqa: E731
    out: dict[str, Any] = {CONNECTOR_KEY: entry.id, "connection_only": True}
    if entry.is_url:
        out.update(transport="http", url=sub(server["url"]).strip())
        headers = {str(k): sub(v) for k, v in (server.get("headers") or {}).items()}
        if headers:
            out["headers"] = headers
        if any(need.kind == "sign-in" for need in entry.needs):
            from clio_agent.gact.mcp_auth import managed_oauth  # noqa: PLC0415

            out["auth"] = managed_oauth(connection_id=entry.id)
    else:
        out.update(
            transport="stdio",
            command=sub(server["command"]),
            args=[sub(arg) for arg in server.get("args") or ()],
        )
        env = {str(k): sub(v) for k, v in (server.get("env") or {}).items()}
        if env:
            out["env"] = env
    return out


def fill_secrets(declared: Mapping[str, Any]) -> dict[str, Any]:
    """A saved connector connection with its secrets filled into headers and env."""

    from clio_agent.connectors import secrets  # noqa: PLC0415

    connector_id = str(declared.get(CONNECTOR_KEY) or "")
    filled = dict(declared)
    for place in ("headers", "env"):
        mapping = declared.get(place)
        if isinstance(mapping, Mapping) and any(
            PLACEHOLDER.search(str(value)) for value in mapping.values()
        ):
            filled[place] = secrets.fill(connector_id, mapping)
    return filled
