"""Catalogs disclosed as skill directories (S4, docs/design/a2ui-compat-
campaign-2026-09.md).

The owner's ruling: producer guidance is progressive disclosure "just like we
do with skills" -- one index line always in context, full component schemas
loaded on demand. Rather than inventing a second disclosure mechanism, every
installed A2UI catalog (builtin ∪ blueprint-declared pack) is exposed to the
existing skill runtime (:mod:`clio_agent.gact.skills`) as a GENERATED skill:
id ``a2ui-catalog-<slug>`` (``a2ui-catalog-basic``, ``a2ui-catalog-clio-
workspace``, or ``a2ui-catalog-<pack-declared-name>``), a body assembled from
the catalog's own files (never written to disk), and a bundled-file root
equal to the catalog's own directory so ``load_skill(id,
file="catalog.json#/components/Button")`` reads the OFFICIAL file the server
validates against -- the catalog file is the allowlist (S2); this module
never re-states component shapes.

This is the ONE place that turns a :class:`~clio_agent.gact.a2ui_catalogs.
registry.CatalogEntry` into a skill id or a skill body; :mod:`clio_agent.gact
.skills` calls back into here (a deferred import, see that module's ``catalog``
scope) and :mod:`clio_agent.gact.agents.skill_runtime` / :mod:`clio_agent.
gact.a2ui_producer._refusal` mint the same id/hint strings by importing
:func:`catalog_skill_id` directly -- no id is ever hand-typed twice.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.skills import SkillRef

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry

#: Every generated catalog skill id starts with this prefix.
CATALOG_SKILL_PREFIX = "a2ui-catalog-"


def catalog_skill_id(entry: "CatalogEntry") -> str:
    """Return the skill id a catalog entry is disclosed under.

    Falls back to a sanitized ``catalogId`` when a pack entry somehow carries
    no ``name`` (defensive only -- :func:`~clio_agent.gact.a2ui_catalogs.
    blueprint.load_blueprint_catalogs` always stamps the declared name).
    """

    slug = entry.name or "".join(
        ch if ch.isalnum() or ch == "-" else "-" for ch in entry.catalog_id.lower()
    ).strip("-")
    return f"{CATALOG_SKILL_PREFIX}{slug}"


def _index_line(name: str, description: str) -> str:
    return f"- `{name}` — {description}" if description else f"- `{name}`"


def _component_lines(file: dict[str, Any]) -> list[str]:
    components = file.get("components")
    if not isinstance(components, dict) or not components:
        return []
    lines = ["", "## Components"]
    for name in sorted(components):
        schema = components[name]
        description = str(schema.get("description") or "") if isinstance(schema, dict) else ""
        lines.append(_index_line(name, description))
    return lines


def _function_return_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ""
    return_schema = properties.get("returnType")
    if not isinstance(return_schema, dict):
        return ""
    return str(return_schema.get("const") or "")


def _function_lines(file: dict[str, Any]) -> list[str]:
    functions = file.get("functions")
    if not isinstance(functions, dict) or not functions:
        return []
    lines = ["", "## Functions"]
    for name in sorted(functions):
        schema = functions[name]
        description = str(schema.get("description") or "") if isinstance(schema, dict) else ""
        return_type = _function_return_type(schema)
        label = f"{name}() -> {return_type}" if return_type else f"{name}()"
        lines.append(_index_line(label, description))
    return lines


def _event_lines(entry: "CatalogEntry") -> list[str]:
    events = entry.sidecar.events
    if not events:
        return []
    lines = ["", "## Declared events"]
    for name in sorted(events):
        route = events[name]
        lines.append(f"- `{name}` -> {route.destination}")
    return lines


def generate_catalog_skill_body(entry: "CatalogEntry") -> str:
    """Render this catalog's generated ``SKILL.md`` text.

    Frontmatter (``name``/``description`` from the catalog file's own
    ``title``/``description``), then the sidecar ``instructions.md`` verbatim,
    then a generated index (components, functions, declared events), then the
    exact ``load_skill(...)`` call for one component's schema -- never the
    component shapes themselves (the catalog file is the allowlist, S2).
    """

    file = entry.file
    skill_id = catalog_skill_id(entry)
    title = str(file.get("title") or entry.catalog_id)
    description = str(file.get("description") or title)
    lines = [
        "---",
        f"name: {skill_id}",
        f"description: {description}",
        "---",
        "",
        f"# {title}",
        "",
        entry.instructions.strip(),
    ]
    lines.extend(_component_lines(file))
    lines.extend(_function_lines(file))
    lines.extend(_event_lines(entry))
    lines.extend(
        [
            "",
            "## Loading one component's schema",
            (
                f'Call `load_skill("{skill_id}", '
                'file="catalog.json#/components/<Name>")` for the exact schema '
                "of one component before producing it -- this index only names "
                "what exists, never its shape."
            ),
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _catalog_skill_ref(entry: "CatalogEntry") -> SkillRef:
    # PRIMARY root = the sidecar directory (entry.root_path): holds
    # catalog.clio.json + instructions.md for every catalog, and catalog.json
    # too for every catalog EXCEPT the vendored Basic catalog, whose
    # catalog.json lives elsewhere (asymmetric layout, ``builtin.py``'s
    # module docstring). SECONDARY root = catalog.json's own parent
    # directory, added to ``extra_dirs`` only when it differs from the
    # primary root -- this is what makes BOTH
    # load_skill(id, file="instructions.md") and
    # load_skill(id, file="catalog.json#/...") work for every catalog,
    # Basic included, without exposing anything outside these two locations.
    root = Path(entry.root_path)
    catalog_dir = Path(entry.catalog_file_path).parent
    extra_dirs = () if catalog_dir == root else (str(catalog_dir),)
    file = entry.file
    skill_id = catalog_skill_id(entry)
    description = str(file.get("description") or file.get("title") or entry.catalog_id)
    return SkillRef(
        id=skill_id,
        title=str(file.get("title") or skill_id),
        description=description,
        # No SKILL.md ever exists on disk for a generated catalog skill -- this
        # is a synthetic identity path (logging/dedup only); the body always
        # comes from ``body_provider``, never ``Path(path).read_text()``.
        path=str(root / "SKILL.md"),
        dir=str(root),
        extra_dirs=extra_dirs,
        scope="catalog",
        source=entry.source,
        layout="skill_md",
        meta={"name": skill_id, "description": description},
        body_provider=functools.partial(generate_catalog_skill_body, entry),
    )


def discover_catalog_skill_refs(entries: "list[CatalogEntry]") -> list[SkillRef]:
    """Return one generated :class:`SkillRef` per given catalog entry.

    The CALLER resolves which entries are in scope
    (:func:`clio_agent.gact.skills.SkillCatalog._catalog_refs` passes this
    session's full PRODUCIBLE set -- builtins ∪ globally-installed packs ∪
    the session's own PATH-activated pack, via
    :func:`~clio_agent.gact.a2ui_catalogs.activation.
    session_producible_catalog_ids` /
    :func:`~clio_agent.gact.a2ui_catalogs.activation.
    session_catalog_resolver`) -- this function only ever RENDERS entries
    into skill refs, never decides producibility (a bare
    ``registry.installed()`` walk would miss a path-activated pack the
    app-level registry's own discovery never sees, silently under-declaring
    catalog skills for exactly that session).
    """

    return [_catalog_skill_ref(entry) for entry in entries]


__all__ = [
    "CATALOG_SKILL_PREFIX",
    "catalog_skill_id",
    "discover_catalog_skill_refs",
    "generate_catalog_skill_body",
]
