"""The server-side A2UI catalog registry — the protocol's unit of trust.

Replaces the single hard-coded ``CLIO_A2UI_CATALOG_ID`` equality check
(deleted, docs/design/a2ui-compat-campaign-2026-09.md S2) with a real
registry keyed ``(catalogId, protocolVersion)``: the two builtin catalogs
(Basic, CLIO workspace) plus every catalog a discovered Agent Blueprint pack
declares. ``CatalogRegistry`` never decides what a session may PRODUCE — that
is ``activation.session_producible_catalog_ids`` — it only answers "does this
id resolve to a catalog this server can validate against."

Validator compilation (``jsonschema.Draft202012Validator``, via
``clio_schemas.a2ui.validation.catalog_validators``) is the expensive part of
loading a catalog, so it is cached by the catalog file's content checksum:
an unchanged pack catalog reuses its compiled validators across every
``.installed()`` call even though blueprint discovery itself re-scans the
filesystem each time (matching ``blueprint_activation.py``'s existing
no-cache-just-rescan pattern for MCP servers).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from clio_schemas.a2ui.sidecar import CatalogSidecar
from clio_schemas.a2ui.validation import catalog_validators
from jsonschema import Draft202012Validator

from clio_agent.gact.protocol.constants import A2UI_V091

CatalogSource = Literal["builtin", "blueprint"]


@dataclass(frozen=True)
class CatalogEntry:
    """One resolved, validator-compiled A2UI catalog.

    Attributes:
        catalog_id: The catalog file's own ``catalogId`` (a stable URI-style
            identifier; the wire's negotiated key).
        protocol_version: The A2UI protocol version this catalog targets
            (``"0.9.1"`` for every catalog in this slice).
        file: The raw official catalog-file document (``$schema``, ``$id``,
            ``catalogId``, ``components``, ``functions``, ``$defs``).
        sidecar: CLIO's packaging metadata for this catalog (kernel
            implementations, event destinations, trust source).
        instructions: The catalog's producer-guidance Markdown.
        source: Where this catalog came from.
        root_path: The directory the catalog's three files were read from.
        checksum: Content checksum of ``file`` (validator cache key).
        validators: One compiled ``Draft202012Validator`` per component name.
    """

    catalog_id: str
    protocol_version: str
    file: dict[str, Any]
    sidecar: CatalogSidecar
    instructions: str
    source: CatalogSource
    root_path: Path
    checksum: str
    validators: dict[str, Draft202012Validator]


class CatalogResolver(Protocol):
    """The minimal shape ``gact/a2ui.py`` validation needs from a registry."""

    def get(self, catalog_id: str, protocol_version: str = A2UI_V091) -> "CatalogEntry | None":
        """Return the catalog entry for ``(catalog_id, protocol_version)``, or ``None``."""
        ...


_VALIDATOR_CACHE: dict[str, dict[str, Draft202012Validator]] = {}
_VALIDATOR_CACHE_LOCK = threading.Lock()


def compiled_validators(checksum: str, file: Mapping[str, Any]) -> dict[str, Draft202012Validator]:
    """Return this catalog file's per-component validators, cached by checksum.

    Args:
        checksum: Content checksum of ``file`` (see
            :func:`clio_agent.gact.a2ui_catalogs.blueprint.catalog_checksum`).
        file: The raw catalog-file document.

    Returns:
        ``{component_name: compiled validator}``, built once per distinct
        checksum and reused thereafter.
    """

    with _VALIDATOR_CACHE_LOCK:
        cached = _VALIDATOR_CACHE.get(checksum)
    if cached is not None:
        return cached
    compiled = catalog_validators(file)
    with _VALIDATOR_CACHE_LOCK:
        return _VALIDATOR_CACHE.setdefault(checksum, compiled)


def make_entry(
    *,
    file: dict[str, Any],
    sidecar: CatalogSidecar,
    instructions: str,
    source: CatalogSource,
    root_path: Path,
    checksum: str,
) -> CatalogEntry:
    """Build one :class:`CatalogEntry`, compiling (or reusing cached) validators."""

    return CatalogEntry(
        catalog_id=str(file["catalogId"]),
        protocol_version=sidecar.protocolVersion,
        file=file,
        sidecar=sidecar,
        instructions=instructions,
        source=source,
        root_path=root_path,
        checksum=checksum,
        validators=compiled_validators(checksum, file),
    )


class CatalogRegistry:
    """Live catalog lookup: builtins (loaded once) plus discovered pack catalogs.

    ``.installed()`` always re-discovers Agent Blueprint packs (cheap — it is
    the same filesystem scan ``blueprint_activation.blueprint_mcp_servers``
    already does on every call) so a newly installed or removed pack is
    reflected on the next lookup without a server restart. The builtin
    catalogs are immutable package data and are loaded exactly once.
    """

    def __init__(self) -> None:
        from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs  # noqa: PLC0415

        self._builtin: list[CatalogEntry] = list(load_builtin_catalogs())

    def builtin(self) -> list[CatalogEntry]:
        """Return the two builtin catalogs (Basic, CLIO workspace)."""

        return list(self._builtin)

    def installed(self) -> list[CatalogEntry]:
        """Return every catalog this server can validate against: builtin ∪ packs."""

        from clio_agent.gact.a2ui_catalogs.blueprint import (  # noqa: PLC0415
            load_all_blueprint_catalogs,
        )

        entries = list(self._builtin)
        entries.extend(load_all_blueprint_catalogs())
        return entries

    def get(self, catalog_id: str, protocol_version: str = A2UI_V091) -> CatalogEntry | None:
        """Return the installed entry for ``(catalog_id, protocol_version)``, or ``None``."""

        if not catalog_id:
            return None
        for entry in self.installed():
            if entry.catalog_id == catalog_id and entry.protocol_version == protocol_version:
                return entry
        return None


__all__ = [
    "CatalogEntry",
    "CatalogRegistry",
    "CatalogResolver",
    "CatalogSource",
    "compiled_validators",
    "make_entry",
]
