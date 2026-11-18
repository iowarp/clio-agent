"""The key a workspace tool fleet is filed under (CV-D3).

A workspace's resident fleet (``ClioAgent._workspace_state()``'s executors) was
keyed by the canonical workspace root alone. A conversation with its own connector
values (:mod:`clio_agent.connectors.overlay`) must not share a server process with
one that has other values, so a fleet is keyed by the root **and** the digest of
the connector values its server set was resolved with:

* no values of its own: the key is the root, and every such conversation of that
  workspace shares the plain fleet, as before;
* values of its own: ``<root>|cv=<digest>``. Two conversations with the same values
  share that fleet.

The namespaces inside stay the connector ids, so tool names, permission grants and
listing caches do not change. Turn leases and residency holds stay keyed by the
root and protect every fleet of that workspace (the conservative side); the reaper
and every reader of the registry split a key with :func:`fleet_root`, and a stdio
server's working folder is always the root.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Between the workspace root and the connector-values digest. A workspace path
#: never contains it on Windows and practically never elsewhere.
FLEET_MARK = "|cv="


def fleet_key(root: str, variant: str) -> str:
    """The registry key of ``root``'s fleet for connector-values ``variant``."""

    return f"{root}{FLEET_MARK}{variant}" if root and variant else root


def fleet_root(key: str) -> str:
    """The workspace root of a registry key (the key itself for a plain fleet)."""

    return (key or "").partition(FLEET_MARK)[0]


def fleet_variant(key: str) -> str:
    """The connector-values digest of a registry key ("" for a plain fleet)."""

    return (key or "").partition(FLEET_MARK)[2]


def keys_at(keys: Iterable[str], root: str) -> list[str]:
    """Every registry key among ``keys`` that belongs to workspace ``root``."""

    return [key for key in keys if root and fleet_root(key) == root]
