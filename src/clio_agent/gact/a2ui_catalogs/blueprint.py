"""Agent Blueprint pack catalog declaration, loading, and install-time validation.

Cloned from ``gact/blueprint_activation.py``'s ``blueprint_server_map`` /
``resolve_active_blueprint_servers`` shape (docs/design/a2ui-compat-campaign-
2026-09.md S2): a pack declares its catalogs in ``AGENT.md`` frontmatter —

.. code-block:: yaml

    a2ui_catalogs:
      earthscope: catalogs/earthscope

exactly as it declares ``mcp_servers`` — and ships them at
``<pack>/catalogs/<name>/{catalog.json, catalog.clio.json, instructions.md}``.
``install_agent_blueprint`` already copies the whole pack tree (including
``catalogs/``); this module reads it back, validates it, and turns it into
:class:`~clio_agent.gact.a2ui_catalogs.registry.CatalogEntry` rows.
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


def blueprint_catalog_map(blueprint: Any) -> dict[str, str]:
    """Return one blueprint's declared ``{name: relative_dir}`` catalog map.

    Mirrors ``blueprint_activation.blueprint_server_map``'s shape for
    ``mcp_servers``: the raw ``a2ui_catalogs`` frontmatter mapping, string-
    keyed and string-valued, or ``{}`` when the blueprint declares none.
    """

    raw = blueprint.metadata.get("a2ui_catalogs")
    if not isinstance(raw, dict):
        return {}
    return {str(name): str(reldir) for name, reldir in raw.items()}


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


def catalog_pack_dir(blueprint: Any, name: str) -> Path:
    """Return the on-disk directory for one of a blueprint's declared catalogs."""

    reldir = blueprint_catalog_map(blueprint).get(name, f"catalogs/{name}")
    return Path(blueprint.root) / reldir


def _load_one(blueprint: Any, name: str, reldir: str) -> tuple[CatalogEntry | None, list[str]]:
    """Load and validate one pack catalog. Returns ``(entry_or_none, errors)``.

    Every structural failure (missing file, schema-invalid ``CatalogFile`` /
    ``CatalogSidecar``, an ``implements[*].kernel`` naming a component no
    builtin catalog implements) is a validation ERROR — refused at
    install/validate time, never silently skipped. A non-UAX#31 component
    name is recorded as a WARNING reason only (0.9.1 tolerates it).
    """

    root = Path(blueprint.root) / reldir
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
        source="blueprint",
        root_path=root,
        checksum=_checksum(file),
    )
    return entry, []


def validate_blueprint_catalogs(blueprint: Any) -> list[str]:
    """Return every a2ui-catalog validation error for one blueprint (empty if clean).

    Called from ``agent_blueprints.validate_agent_blueprint_path`` so a pack
    with a malformed or unimplemented catalog is refused at install/validate
    time, matching how ``_validate_agent_tool_references`` refuses an
    undeclared MCP tool reference.
    """

    errors: list[str] = []
    for name, reldir in blueprint_catalog_map(blueprint).items():
        _, load_errors = _load_one(blueprint, name, reldir)
        errors.extend(load_errors)
    return errors


def load_blueprint_catalogs(blueprint: Any) -> list[CatalogEntry]:
    """Return the successfully-loaded catalog entries one blueprint declares.

    A catalog that fails validation is dropped (its reason was already
    recorded by :func:`_load_one`) rather than raised — a registry read must
    never crash the server over one broken pack; ``validate_agent_blueprint_path``
    is the door that refuses installing/enabling such a pack in the first place.
    """

    entries: list[CatalogEntry] = []
    for name, reldir in blueprint_catalog_map(blueprint).items():
        entry, errors = _load_one(blueprint, name, reldir)
        if entry is not None and not errors:
            entries.append(entry)
    return entries


def load_all_blueprint_catalogs() -> list[CatalogEntry]:
    """Return every declared catalog from every discovered Agent Blueprint.

    Discovery re-scans the filesystem each call (same live-rescan contract as
    ``blueprint_activation.blueprint_mcp_servers``), so a freshly installed or
    removed pack is reflected on the next registry lookup.
    """

    from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

    entries: list[CatalogEntry] = []
    try:
        blueprints = discover_agent_blueprints()
    except Exception:  # noqa: BLE001 - a broken discovery must not crash the registry
        return entries
    for blueprint in blueprints:
        if not blueprint.enabled or not blueprint_catalog_map(blueprint):
            continue
        entries.extend(load_blueprint_catalogs(blueprint))
    return entries


def validate_expert_a2ui_catalogs(
    expert_selection: list[str], declared_catalog_names: set[str]
) -> list[str]:
    """Validate one expert's ``a2ui_catalogs: [names]`` subset declaration.

    Mirrors ``tools.mcp_config.resolve_expert_servers``'s list-selection
    contract: a name outside the blueprint's own declared set is a
    validation error, never silently dropped.
    """

    return [
        f"expert references undeclared a2ui catalog {name!r}"
        for name in expert_selection
        if name not in declared_catalog_names
    ]


def blueprint_and_expert_a2ui_catalog_errors(blueprint: Any, experts: list[Any]) -> list[str]:
    """Return every a2ui-catalog validation error for one blueprint AND its experts.

    Single entry point for ``agent_blueprints.validate_agent_blueprint_path``
    (no-accretion: that module is already at its file-size ratchet baseline)
    -- combines :func:`validate_blueprint_catalogs` with each expert's own
    ``a2ui_catalogs: [names]`` subset check.
    """

    errors = validate_blueprint_catalogs(blueprint)
    declared_catalog_names = set(blueprint_catalog_map(blueprint))
    for row in experts:
        expert_catalogs = row.metadata.get("a2ui_catalogs")
        if isinstance(expert_catalogs, list) and expert_catalogs:
            errors.extend(
                f"{row.id}: {error}"
                for error in validate_expert_a2ui_catalogs(
                    [str(name) for name in expert_catalogs], declared_catalog_names
                )
            )
    return errors


__all__ = [
    "blueprint_and_expert_a2ui_catalog_errors",
    "blueprint_catalog_map",
    "catalog_pack_dir",
    "load_all_blueprint_catalogs",
    "load_blueprint_catalogs",
    "validate_blueprint_catalogs",
    "validate_expert_a2ui_catalogs",
]
