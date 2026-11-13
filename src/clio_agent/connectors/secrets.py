"""Connector secrets (CN8): kept in CLIO's connection credential store.

The store is ``<user config>/mcp_oauth_tokens.json`` (owner-only), the file that
already holds each saved connection's sign-in tokens
(:class:`~clio_agent.tools.mcp_oauth_storage.DurableFileTokenStorage`). A
connector's secrets sit in their own entry of that file, keyed by the connector.
They are read only when a server is started or connected (:func:`fill`), are
never returned to the UI (a row says only whether one is set), and are written
into the person's ``mcp.yaml`` only as ``${need.<key>}`` placeholders.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.connectors.entries import PLACEHOLDER
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage

#: The store entry's server key: not a URL, so it never collides with a sign-in.
_SERVER_KEY = "clio-connector-secrets"


def _store(connector_id: str) -> DurableFileTokenStorage:
    return DurableFileTokenStorage(_SERVER_KEY, connection_id=connector_id)


def values(connector_id: str) -> dict[str, str]:
    """The connector's stored secrets by need key."""

    return _store(connector_id).secret_values()


def has(connector_id: str, key: str) -> bool:
    return bool(values(connector_id).get(key))


def save(connector_id: str, changes: Mapping[str, str | None]) -> None:
    """Store or (``None``/empty) forget secrets."""

    _store(connector_id).set_secret_values(dict(changes))


def forget(connector_id: str) -> None:
    save(connector_id, dict.fromkeys(values(connector_id)))


def fill(connector_id: str, mapping: Mapping[str, Any]) -> dict[str, str]:
    """``mapping`` (headers or env) with each ``${need.<key>}`` replaced by its secret.

    A missing secret becomes empty: the connector's state says what it needs, and a
    call waits for it (CN5) rather than a server starting with a placeholder.
    """

    stored = values(connector_id)
    return {
        str(key): PLACEHOLDER.sub(lambda match: stored.get(match.group(1), ""), str(value))
        for key, value in mapping.items()
    }
