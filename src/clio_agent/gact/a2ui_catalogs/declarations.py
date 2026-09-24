"""Per-agent A2UI catalog declarations and their ordered resolution (v15 S8).

An agent's ``a2ui_catalogs`` is the COMPLETE allowlist of A2UI catalogs it may
produce against. Nothing is implicit: the two builtins (``clio-workspace``,
``basic``) are available to an agent only when it lists them, exactly like a
pack-local catalog. The written order is the preference order -- the first
declared catalog a client supports is the one an unnamed surface gets.

**Forward shape (agent-plugins 1.0).** Today an agent is one Agent Blueprint,
so there is exactly one declaration *source*: the active blueprint's own
``a2ui_catalogs``. Agent-plugins will replace blueprints, and an agent will be
a concatenation of plugins, each declaring its own catalogs. Resolution is
therefore written over an ORDERED SEQUENCE of sources
(:func:`resolve_agent_catalogs`), never over "the blueprint": a plugin becomes
one more :class:`CatalogDeclarationSource` and no consumer changes. Each
:class:`CatalogDeclaration` is self-contained -- its name plus its origin
(a builtin, or a directory already resolved against ITS OWN declaring unit's
root) -- so declarations from different units can be merged without knowing
which unit a relative path was relative to.

Merge rules (the ordered union):

* Order is source order, then declaration order within a source.
* The same name with the same origin (the same builtin, or the same resolved
  directory) is deduplicated -- the first position wins.
* The same name with a DIFFERENT origin, or two names resolving to the same
  ``catalogId``, is a typed conflict (``a2ui_catalog_declaration_conflict``):
  the later declaration is refused, never a silent override.

Accepted frontmatter forms (``AGENT.md``):

.. code-block:: yaml

    # Canonical: an ordered list. A bare string names a builtin; a one-key
    # mapping is a pack-local catalog directory relative to the pack root.
    a2ui_catalogs:
      - earthscope-stations: catalogs/earthscope-stations
      - clio-workspace

    # Legacy (pre-S8) mapping form: every entry is a pack-local directory,
    # in written order. It can never name a builtin.
    a2ui_catalogs:
      earthscope-stations: catalogs/earthscope-stations

Every consumer that decides producibility or disclosure derives from ONE
resolution (``activation.resolve_session_catalogs``) built on this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact.a2ui_catalogs.reasons import record_a2ui_catalog_reason_once

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry

#: Where one declared catalog comes from.
CatalogOrigin = Literal["builtin", "directory"]

#: The kind of unit that declared a source: an Agent Blueprint, or a
#: code-shipped agent (the builtin main, ``catalog.builtin_main_catalog_source``).
#: ``"plugin"`` joins with agent-plugins 1.0; nothing downstream switches on it.
DeclaringUnitKind = Literal["blueprint", "builtin_agent"]


@dataclass(frozen=True)
class CatalogDeclarationIssue:
    """One typed problem found while parsing or resolving declarations.

    Attributes:
        reason: A key of the A2UI catalog reason catalog (``reasons.py``).
        name: The catalog name the issue is about (``""`` when unnamed).
        unit_id: The declaring unit (``"blueprint:<id>"``).
        detail: A human-readable, specific description.
    """

    reason: str
    name: str
    unit_id: str
    detail: str

    def message(self) -> str:
        """Return the validation-error string for this issue."""

        return f"a2ui catalog {self.name!r} ({self.unit_id}): {self.detail}"


@dataclass(frozen=True)
class CatalogDeclaration:
    """One self-contained catalog declaration.

    Attributes:
        name: The catalog's local name (the builtin's name, or the pack key).
        origin: ``"builtin"`` or ``"directory"``.
        directory: For ``origin == "directory"``, the catalog directory
            already resolved against its declaring unit's root; ``None`` for
            a builtin.
    """

    name: str
    origin: CatalogOrigin
    directory: Path | None = None

    def identity(self) -> tuple[str, str]:
        """Return ``(origin, location)`` -- equal identities deduplicate."""

        if self.directory is None:
            return (self.origin, "")
        return (self.origin, str(self.directory.resolve()))


@dataclass(frozen=True)
class CatalogDeclarationSource:
    """One declaring unit's ordered catalog declarations.

    Attributes:
        unit_kind: What kind of unit declared these (today: ``"blueprint"``).
        unit_id: A stable label for the unit (``"blueprint:<id>"``).
        declared: Whether the unit declared ``a2ui_catalogs`` at all (an
            explicit empty list is a declaration of zero catalogs).
        declarations: The parsed declarations, in written order.
        install_checksum: The unit's own install checksum, stamped onto the
            directory-origin entries it loads.
        parse_errors: Typed issues found while parsing the raw declaration.
        unresolved: The unit is bound but could not be loaded (a session's
            blueprint id that resolves to nothing), so what it declares is
            unknown -- distinct from a unit that declares nothing.
    """

    unit_kind: DeclaringUnitKind
    unit_id: str
    declared: bool
    declarations: tuple[CatalogDeclaration, ...] = ()
    install_checksum: str = ""
    parse_errors: tuple[CatalogDeclarationIssue, ...] = ()
    unresolved: bool = False


@dataclass(frozen=True)
class ResolvedCatalogs:
    """The ordered, deduplicated result of :func:`resolve_agent_catalogs`.

    Attributes:
        entries: The loaded catalogs, in preference (declaration) order.
        declared: Whether ANY source declared ``a2ui_catalogs``.
        issues: Every typed parse/resolution issue, in encounter order.
        attempted: Whether any source declared at least one ENTRY (well-formed
            or not). An explicit ``a2ui_catalogs: []`` declares nothing.
    """

    entries: tuple["CatalogEntry", ...] = ()
    declared: bool = False
    issues: tuple[CatalogDeclarationIssue, ...] = field(default_factory=tuple)
    attempted: bool = False
    unresolved: bool = False

    @property
    def catalog_ids(self) -> tuple[str, ...]:
        """The resolved catalog ids, in preference order."""

        return tuple(entry.catalog_id for entry in self.entries)

    def get(self, catalog_id: str, protocol_version: str) -> "CatalogEntry | None":
        """Return the resolved entry for ``(catalog_id, protocol_version)``, or ``None``."""

        return next(
            (
                entry
                for entry in self.entries
                if entry.catalog_id == catalog_id and entry.protocol_version == protocol_version
            ),
            None,
        )

    def empty_reason(self) -> str | None:
        """The typed reason this resolution has no catalogs, or ``None`` if it has some."""

        if self.entries:
            return None
        if self.unresolved:
            return "a2ui_blueprint_unresolved"
        return "a2ui_no_catalogs_resolved" if self.attempted else "a2ui_no_catalogs_declared"


def builtin_catalog_names() -> tuple[str, ...]:
    """Return the names a declaration may use to reference a builtin catalog."""

    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs  # noqa: PLC0415

    return tuple(entry.name for entry in load_builtin_catalogs())


def _issue(reason: str, name: str, unit_id: str, detail: str) -> CatalogDeclarationIssue:
    return CatalogDeclarationIssue(reason=reason, name=name, unit_id=unit_id, detail=detail)


def _parse_item(
    item: Any, *, unit_id: str, root: Path, builtins: tuple[str, ...]
) -> tuple[CatalogDeclaration | None, CatalogDeclarationIssue | None]:
    """Parse one list-form entry: a builtin name, or a one-key ``{name: reldir}``."""

    if isinstance(item, str):
        name = item.strip()
        if name in builtins:
            return CatalogDeclaration(name=name, origin="builtin"), None
        return None, _issue(
            "a2ui_catalog_builtin_unknown",
            name,
            unit_id,
            f"is not a builtin catalog (builtins: {', '.join(builtins)}); a pack-local "
            "catalog is declared as `name: relative/dir`",
        )
    if isinstance(item, dict) and len(item) == 1:
        ((raw_name, raw_dir),) = item.items()
        return _directory_declaration(str(raw_name).strip(), raw_dir, unit_id=unit_id, root=root)
    return None, _issue(
        "a2ui_catalog_declaration_invalid",
        "",
        unit_id,
        f"entry {item!r} is neither a builtin name nor a single `name: relative/dir` mapping",
    )


def _directory_declaration(
    name: str, raw_dir: Any, *, unit_id: str, root: Path
) -> tuple[CatalogDeclaration | None, CatalogDeclarationIssue | None]:
    if not name or not isinstance(raw_dir, str) or not raw_dir.strip():
        return None, _issue(
            "a2ui_catalog_declaration_invalid",
            name,
            unit_id,
            f"a pack-local catalog needs a non-empty relative directory, got {raw_dir!r}",
        )
    return CatalogDeclaration(name=name, origin="directory", directory=root / raw_dir.strip()), None


def parse_catalog_declarations(
    raw: Any,
    *,
    unit_kind: DeclaringUnitKind,
    unit_id: str,
    root: Path,
    install_checksum: str = "",
) -> CatalogDeclarationSource:
    """Parse one unit's raw ``a2ui_catalogs`` value into a declaration source.

    Args:
        raw: The frontmatter value verbatim (``None`` when absent).
        unit_kind: The declaring unit's kind.
        unit_id: A stable label for the declaring unit.
        root: The declaring unit's root directory; relative catalog
            directories resolve against it.
        install_checksum: The declaring unit's install checksum.

    Returns:
        The parsed source. Malformed entries become typed ``parse_errors``,
        never silently dropped.
    """

    if raw is None:
        return CatalogDeclarationSource(unit_kind=unit_kind, unit_id=unit_id, declared=False)
    builtins = builtin_catalog_names()
    declarations: list[CatalogDeclaration] = []
    errors: list[CatalogDeclarationIssue] = []
    if isinstance(raw, list):
        pairs = [_parse_item(item, unit_id=unit_id, root=root, builtins=builtins) for item in raw]
    elif isinstance(raw, dict):
        pairs = [
            _directory_declaration(str(name).strip(), reldir, unit_id=unit_id, root=root)
            for name, reldir in raw.items()
        ]
    else:
        pairs = [
            (
                None,
                _issue(
                    "a2ui_catalog_declaration_invalid",
                    "",
                    unit_id,
                    f"a2ui_catalogs must be a list (or a legacy mapping), got {type(raw).__name__}",
                ),
            )
        ]
    for declaration, error in pairs:
        if declaration is not None:
            declarations.append(declaration)
        if error is not None:
            errors.append(error)
    return CatalogDeclarationSource(
        unit_kind=unit_kind,
        unit_id=unit_id,
        declared=True,
        declarations=tuple(declarations),
        install_checksum=install_checksum,
        parse_errors=tuple(errors),
    )


def blueprint_catalog_source(blueprint: Any) -> CatalogDeclarationSource:
    """Return one Agent Blueprint's catalog declarations as a source.

    The blueprint's ``metadata["a2ui_catalogs"]`` is the frontmatter value
    verbatim (``agent_blueprints.parse_agent_blueprint_root``); relative
    directories resolve against the blueprint root.
    """

    metadata = blueprint.metadata if isinstance(blueprint.metadata, dict) else {}
    install = metadata.get("install")
    install_checksum = str(install.get("checksum") or "") if isinstance(install, dict) else ""
    return parse_catalog_declarations(
        metadata.get("a2ui_catalogs"),
        unit_kind="blueprint",
        unit_id=f"blueprint:{blueprint.id}",
        root=Path(blueprint.root),
        install_checksum=install_checksum,
    )


#: Guidance shown to the user for an agent that declares no A2UI catalogs.
MISSING_DECLARATION_GUIDANCE = (
    "This agent predates per-agent A2UI catalogs, so it cannot create interactive "
    "views. Update it, or add an a2ui_catalogs list (for example `- clio-workspace`) "
    "to its AGENT.md."
)


def a2ui_declaration_notice(blueprint: Any) -> dict[str, str] | None:
    """A user-visible notice for a blueprint that declares no ``a2ui_catalogs``.

    Such an agent has no catalogs under the strict rule (nothing is implicit);
    typically it is an installed snapshot the upgrade re-sync skipped (locally
    edited, foreign source, pinned, or the re-sync failed). Returns ``None``
    for a blueprint that declares its catalogs (even an explicit empty list).
    """

    if blueprint_catalog_source(blueprint).declared:
        return None
    return {
        "reason": "a2ui_declaration_missing_after_upgrade",
        "detail": MISSING_DECLARATION_GUIDANCE,
    }


def _load_declaration(
    declaration: CatalogDeclaration, source: CatalogDeclarationSource
) -> tuple["CatalogEntry | None", list[str]]:
    if declaration.origin == "builtin":
        from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs  # noqa: PLC0415

        entry = next((row for row in load_builtin_catalogs() if row.name == declaration.name), None)
        if entry is None:  # parse already refused unknown builtin names
            return None, [f"builtin catalog {declaration.name!r} is not shipped"]
        return entry, []
    from clio_agent.gact.a2ui_catalogs.blueprint import load_catalog_directory  # noqa: PLC0415

    assert declaration.directory is not None
    return load_catalog_directory(
        declaration.directory, declaration.name, install_checksum=source.install_checksum
    )


def resolve_agent_catalogs(
    sources: Sequence[CatalogDeclarationSource], *, record: bool = False
) -> ResolvedCatalogs:
    """Resolve an agent's catalogs as the ordered union of its declaration sources.

    Args:
        sources: The agent's declaration sources, in precedence order. Today
            this is at most one (the active blueprint); agent-plugins add
            more without changing any consumer.
        record: Whether to write each issue to the typed reason ledger,
            once per (unit, reason, catalog) for the process. Session callers
            leave it ``False`` and record per session instead
            (``activation.resolve_session_catalogs``); validation reports
            issues as errors itself.

    Returns:
        The ordered, deduplicated resolution. Conflicts and load failures are
        typed ``issues``; the offending declaration is refused, never merged.
    """

    entries: list["CatalogEntry"] = []
    issues: list[CatalogDeclarationIssue] = []
    by_name: dict[str, tuple[tuple[str, str], str]] = {}
    by_catalog_id: dict[str, str] = {}
    declared = False
    attempted = False
    unresolved = any(source.unresolved for source in sources)
    for source in sources:
        declared = declared or source.declared
        attempted = attempted or bool(source.declarations or source.parse_errors)
        issues.extend(source.parse_errors)
        for declaration in source.declarations:
            prior = by_name.get(declaration.name)
            if prior is not None:
                if prior[0] != declaration.identity():
                    issues.append(
                        _issue(
                            "a2ui_catalog_declaration_conflict",
                            declaration.name,
                            source.unit_id,
                            f"is already declared by {prior[1]} with a different origin "
                            f"({prior[0][0]} {prior[0][1]!r} vs {declaration.identity()[0]} "
                            f"{declaration.identity()[1]!r})",
                        )
                    )
                continue
            entry, load_errors = _load_declaration(declaration, source)
            if entry is None:
                issues.extend(
                    _issue("a2ui_catalog_declaration_invalid", declaration.name, source.unit_id, e)
                    for e in load_errors
                )
                continue
            owner = by_catalog_id.get(entry.catalog_id)
            if owner is not None:
                issues.append(
                    _issue(
                        "a2ui_catalog_declaration_conflict",
                        declaration.name,
                        source.unit_id,
                        f"resolves to catalogId {entry.catalog_id!r}, already declared as {owner!r}",
                    )
                )
                continue
            by_name[declaration.name] = (declaration.identity(), source.unit_id)
            by_catalog_id[entry.catalog_id] = declaration.name
            entries.append(entry)
    if record:
        for item in issues:
            record_a2ui_catalog_reason_once(
                (item.unit_id, item.reason, item.name),
                item.reason,
                catalog=item.name,
                unit=item.unit_id,
                detail=item.detail,
            )
    return ResolvedCatalogs(
        entries=tuple(entries),
        declared=declared,
        issues=tuple(issues),
        attempted=attempted,
        unresolved=unresolved,
    )


__all__ = [
    "MISSING_DECLARATION_GUIDANCE",
    "a2ui_declaration_notice",
    "CatalogDeclaration",
    "CatalogDeclarationIssue",
    "CatalogDeclarationSource",
    "CatalogOrigin",
    "DeclaringUnitKind",
    "ResolvedCatalogs",
    "blueprint_catalog_source",
    "builtin_catalog_names",
    "parse_catalog_declarations",
    "resolve_agent_catalogs",
]
