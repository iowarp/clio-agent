"""A2UI catalogs contributed by resolved plugins.

A plugin names built-in catalogs in ``ai.iowarp.clio.catalogs`` (``core``
declares ``clio-workspace``) or ships its own under ``ai.iowarp.clio/a2ui/<name>/``
(``catalog.json``, ``catalog.clio.json``, ``instructions.md``). Resolution keeps
them in plugin order; this module loads them for the session and for the
generated half of ``core.rendering``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from clio_schemas.a2ui.sidecar import CatalogSidecar
from clio_schemas.a2ui.v0_9_1.catalog_file import CatalogFile
from pydantic import ValidationError

from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs
from clio_agent.gact.a2ui_catalogs.reasons import record_a2ui_catalog_reason
from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, make_entry

_CATALOG_FILES = ("catalog.json", "catalog.clio.json", "instructions.md")


def _checksum(file: dict[str, Any]) -> str:
    encoded = json.dumps(file, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _implemented_component_names() -> frozenset[str]:
    """Every component name a builtin catalog's renderer kernel implements."""

    names: set[str] = set()
    for entry in load_builtin_catalogs():
        names.update(entry.sidecar.implements)
    return frozenset(names)


def _non_uax31_names(file: dict[str, Any]) -> list[str]:
    """Component names that are not valid UAX#31 identifiers (0.9.1: warning only)."""

    return [name for name in file.get("components", {}) if not str(name).isidentifier()]


def load_catalog_directory(
    root: Path, name: str, *, install_checksum: str = ""
) -> tuple[CatalogEntry | None, list[str]]:
    """Load and validate one catalog directory. Returns ``(entry_or_none, errors)``.

    ``root`` is the catalog directory itself, already resolved against its
    declaring unit (``declarations.CatalogDeclaration.directory``);
    ``install_checksum`` is the declaring plugin's content digest, so a
    consumer can tell which plugin version a catalog came from without
    re-hashing the catalog file.

    Every structural failure (missing file, schema-invalid ``CatalogFile`` /
    ``CatalogSidecar``, an ``implements[*].kernel`` naming a component no
    builtin catalog implements) is a validation ERROR — refused at
    install/validate time, never silently skipped. A non-UAX#31 component
    name is recorded as a WARNING reason only (0.9.1 tolerates it).
    """

    errors: list[str] = []
    missing = [name for name in _CATALOG_FILES if not (root / name).is_file()]
    if missing:
        return None, [f"a2ui catalog {name!r} is missing files: {', '.join(missing)}"]
    try:
        file = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        CatalogFile.model_validate(file)
    except (OSError, ValueError, ValidationError) as exc:
        record_a2ui_catalog_reason("a2ui_catalog_file_invalid", catalog=name, detail=str(exc))
        return None, [f"a2ui catalog {name!r} catalog.json is invalid: {exc}"]
    try:
        sidecar_raw = json.loads((root / "catalog.clio.json").read_text(encoding="utf-8"))
        sidecar = CatalogSidecar.model_validate(sidecar_raw)
    except (OSError, ValueError, ValidationError) as exc:
        record_a2ui_catalog_reason("a2ui_sidecar_invalid", catalog=name, detail=str(exc))
        return None, [f"a2ui catalog {name!r} catalog.clio.json is invalid: {exc}"]
    implemented = _implemented_component_names()
    unimplemented = {
        component_name: impl.kernel
        for component_name, impl in sidecar.implements.items()
        if impl.kernel not in implemented
    }
    if unimplemented:
        record_a2ui_catalog_reason(
            "a2ui_component_unimplemented", catalog=name, components=sorted(unimplemented)
        )
        errors.append(
            f"a2ui catalog {name!r} implements[].kernel names components no builtin "
            f"catalog implements: {unimplemented}"
        )
    for bad_name in _non_uax31_names(file):
        record_a2ui_catalog_reason("a2ui_identifier_not_uax31", catalog=name, component=bad_name)
    if errors:
        return None, errors
    instructions = (root / "instructions.md").read_text(encoding="utf-8")
    entry = make_entry(
        file=file,
        sidecar=sidecar,
        instructions=instructions,
        source="plugin",
        root_path=root,
        checksum=_checksum(file),
        install_checksum=install_checksum,
        name=name,
    )
    return entry, []




def load_plugin_catalog(name: str, path: str) -> tuple[CatalogEntry | None, str]:
    """Load one catalog a plugin declared. Returns ``(entry, error)``."""

    if not path:
        for entry in load_builtin_catalogs():
            if entry.name == name or entry.catalog_id == name:
                return entry, ""
        return None, f"no built-in A2UI catalog named {name!r}"
    entry, errors = load_catalog_directory(Path(path), name)
    return entry, "; ".join(errors)


def catalog_components(name: str, path: str) -> tuple[list[str], str]:
    """The component names a declared catalog offers, or why it cannot load."""

    entry, error = load_plugin_catalog(name, path)
    if entry is None:
        return [], error or "catalog unavailable"
    return sorted((entry.file.get("components") or {}).keys()), ""


def installed_plugin_catalogs(records: list[Any]) -> list[CatalogEntry]:
    """Every valid catalog shipped in an installed plugin's ``a2ui/`` folder."""

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for record in records:
        for catalog in record.catalogs:
            if not catalog.path:
                continue
            entry, errors = load_catalog_directory(Path(catalog.path), catalog.id)
            if entry is None or entry.catalog_id in seen:
                continue
            seen.add(entry.catalog_id)
            entries.append(entry)
    return entries
