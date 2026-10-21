"""A2UI catalogs contributed by resolved plugins.

A plugin names built-in catalogs in ``ai.iowarp.clio.catalogs`` (``core``
declares ``clio-workspace``) or ships its own under ``ai.iowarp.clio/a2ui/<name>/``
(``catalog.json``, ``catalog.clio.json``, ``instructions.md``). Resolution keeps
them in plugin order; this module loads them for the session and for the
generated half of ``core.rendering``.
"""

from __future__ import annotations

from pathlib import Path

from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs
from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry


def load_plugin_catalog(name: str, path: str) -> tuple[CatalogEntry | None, str]:
    """Load one catalog a plugin declared. Returns ``(entry, error)``."""

    if not path:
        for entry in load_builtin_catalogs():
            if entry.name == name or entry.catalog_id == name:
                return entry, ""
        return None, f"no built-in A2UI catalog named {name!r}"
    from clio_agent.gact.a2ui_catalogs.blueprint import load_catalog_directory  # noqa: PLC0415

    entry, errors = load_catalog_directory(Path(path), name)
    return entry, "; ".join(errors)


def catalog_components(name: str, path: str) -> tuple[list[str], str]:
    """The component names a declared catalog offers, or why it cannot load."""

    entry, error = load_plugin_catalog(name, path)
    if entry is None:
        return [], error or "catalog unavailable"
    return sorted((entry.file.get("components") or {}).keys()), ""
