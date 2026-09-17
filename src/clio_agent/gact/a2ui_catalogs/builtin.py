"""The two catalogs CLIO ships in-process: Basic and CLIO workspace.

Loaded from clio-schemas' package data via ``importlib.resources`` (never
regenerated at runtime — the committed bytes ARE the contract, matching
clio-schemas' own "consumers copy immutable bytes" export design). The
layout is asymmetric on disk and this module follows it rather than
flattening it: the vendored upstream Basic ``catalog.json`` lives beside the
rest of the vendored 0.9.1 spec (``a2ui/v0_9_1/catalogs/basic/catalog.json``,
untouched upstream bytes), while its CLIO sidecar and instructions — and all
three of the CLIO workspace catalog's files — live under the CLIO-owned
``a2ui/catalogs/**`` tree. Both catalogs' own ``catalogId`` (read from their
``catalog.json``) is the single source of truth for that id; nothing here
hardcodes it a second time.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
import json
from pathlib import Path
from typing import Any

from clio_schemas.a2ui.sidecar import CatalogSidecar

from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry, make_entry

_BASIC_CATALOG_FILE_RELPATH = "a2ui/v0_9_1/catalogs/basic/catalog.json"
_BASIC_SIDECAR_DIR = "a2ui/catalogs/basic"
_WORKSPACE_DIR = "a2ui/catalogs/clio-workspace/v1"


def _resource_root() -> Path:
    """Filesystem path to clio-schemas' package data root."""

    return Path(str(importlib.resources.files("clio_schemas") / "schemas"))


def _read_json(relpath: str) -> dict[str, Any]:
    path = _resource_root() / relpath
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(relpath: str) -> str:
    path = _resource_root() / relpath
    return path.read_text(encoding="utf-8")


def _checksum(file: dict[str, Any]) -> str:
    encoded = json.dumps(file, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_basic() -> CatalogEntry:
    file = _read_json(_BASIC_CATALOG_FILE_RELPATH)
    sidecar = CatalogSidecar.model_validate(_read_json(f"{_BASIC_SIDECAR_DIR}/catalog.clio.json"))
    instructions = _read_text(f"{_BASIC_SIDECAR_DIR}/instructions.md")
    return make_entry(
        file=file,
        sidecar=sidecar,
        instructions=instructions,
        source="builtin",
        root_path=_resource_root() / _BASIC_SIDECAR_DIR,
        checksum=_checksum(file),
    )


def _load_workspace() -> CatalogEntry:
    file = _read_json(f"{_WORKSPACE_DIR}/catalog.json")
    sidecar = CatalogSidecar.model_validate(_read_json(f"{_WORKSPACE_DIR}/catalog.clio.json"))
    instructions = _read_text(f"{_WORKSPACE_DIR}/instructions.md")
    return make_entry(
        file=file,
        sidecar=sidecar,
        instructions=instructions,
        source="builtin",
        root_path=_resource_root() / _WORKSPACE_DIR,
        checksum=_checksum(file),
    )


@functools.lru_cache(maxsize=1)
def load_builtin_catalogs() -> tuple[CatalogEntry, ...]:
    """Return the two builtin catalogs, loaded once and cached for the process.

    Returns:
        ``(basic_entry, workspace_entry)`` — immutable package data, so a
        single process-wide load is safe and avoids re-reading + re-compiling
        validators on every registry construction.
    """

    return (_load_basic(), _load_workspace())


def workspace_catalog_id() -> str:
    """Return the CLIO workspace catalog's own ``catalogId`` (self-describing)."""

    return load_builtin_catalogs()[1].catalog_id


def basic_catalog_id() -> str:
    """Return the vendored Basic catalog's own ``catalogId`` (self-describing)."""

    return load_builtin_catalogs()[0].catalog_id


__all__ = [
    "basic_catalog_id",
    "load_builtin_catalogs",
    "workspace_catalog_id",
]
