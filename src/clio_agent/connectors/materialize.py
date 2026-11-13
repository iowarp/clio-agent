"""An enabled connector as the person's saved MCP connection (CN1, CN2, CN8).

The connection has the connector's id as its name, ``connector: <id>`` as its
marker and ``connection_only: true``: it is never mounted on its own, only through
the connector's plugin (:mod:`.follow`), which a person adds to a conversation.
The saved declaration is a template: every need stays a ``${need.<key>}``
placeholder (CV-D2). Address and value needs are filled per conversation, the
conversation's own values over the person's defaults (:func:`fill_declaration`,
called by ``plugins.servers.server_declarations``); anything still unfilled when
a spec is parsed takes the defaults (``tools.mcp_config.spec_from_declaration``).
A secret is filled only when the server starts (:func:`fill_secrets`).
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


def declaration(entry: Entry) -> dict[str, Any]:
    """The saved-connection template for ``entry``: every need a ``${need.<key>}``."""

    server = entry.server
    out: dict[str, Any] = {CONNECTOR_KEY: entry.id, "connection_only": True}
    if entry.is_url:
        out.update(transport="http", url=str(server["url"]).strip())
        headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
        if headers:
            out["headers"] = headers
        if any(need.kind == "sign-in" for need in entry.needs):
            from clio_agent.gact.mcp_auth import managed_oauth  # noqa: PLC0415

            out["auth"] = managed_oauth(connection_id=entry.id)
    else:
        out.update(
            transport="stdio",
            command=str(server["command"]),
            args=[str(arg) for arg in server.get("args") or ()],
        )
        env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
        if env:
            out["env"] = env
    return out


def _fill(entry: Entry, declared: Mapping[str, Any], values: Mapping[str, str]) -> dict:
    sub = lambda text: _substitute(entry, values, str(text), keep_secrets=True)  # noqa: E731
    out = dict(declared)
    for place in ("url", "command"):
        if isinstance(out.get(place), str):
            out[place] = sub(out[place]).strip() if place == "url" else sub(out[place])
    if isinstance(out.get("args"), list):
        out["args"] = [sub(arg) for arg in out["args"]]
    for place in ("headers", "env"):
        if isinstance(out.get(place), Mapping):
            out[place] = {str(k): sub(v) for k, v in out[place].items()}
    return out


def resolved(entry: Entry, values: Mapping[str, str]) -> dict[str, Any]:
    """The declaration with ``values`` written in (secrets stay placeholders)."""

    return _fill(entry, declaration(entry), values)


def fill_declaration(
    declared: Mapping[str, Any],
    overlay: Mapping[str, str] | None = None,
    *,
    from_entry: bool = False,
) -> dict[str, Any]:
    """A connector connection with its address and value needs filled.

    Each need takes ``overlay`` (one conversation's values) over the person's defaults;
    what is already written in stays. With ``from_entry`` the connector's current
    entry is the template, so a connection saved by an older CLIO (values written in)
    follows the conversation too; its sign-in block is kept. A declaration with no
    known connector is returned unchanged.
    """

    from clio_agent.connectors import state  # noqa: PLC0415

    connector_id = str(declared.get(CONNECTOR_KEY) or "")
    entry = state.all_entries().get(connector_id) if connector_id else None
    if entry is None:
        return dict(declared)
    template = dict(declared)
    if from_entry:
        template = declaration(entry)
        if declared.get("auth") and template.get("auth"):
            template["auth"] = declared["auth"]
    values = {**state.choice(entry.id).values, **dict(overlay or {})}
    return _fill(entry, template, values)


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


def connector_spec(spec: Any, connector_id: str) -> Any:
    """A parsed spec of a connector's connection, its secrets filled after ${VAR} expansion."""

    import dataclasses  # noqa: PLC0415

    filled = fill_secrets({CONNECTOR_KEY: connector_id, "env": spec.env, "headers": spec.headers})
    return dataclasses.replace(
        spec, env=filled["env"], headers=filled["headers"], connector=connector_id
    )
