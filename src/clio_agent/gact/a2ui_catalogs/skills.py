"""Catalogs disclosed as skill directories (S4, docs/design/a2ui-compat-
campaign-2026-09.md).

The owner's ruling: producer guidance is progressive disclosure "just like we
do with skills" -- one index line always in context, full component schemas
loaded on demand. Rather than inventing a second disclosure mechanism, every
A2UI catalog (builtin or blueprint-declared pack) can be exposed to the
existing skill runtime (:mod:`clio_agent.gact.skills`) as a GENERATED skill
(disclosed to a session only when its agent declares that catalog, v15 S8):
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
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_catalogs.component_signature import generate_component_signature
from clio_agent.gact.a2ui_catalogs.media_sources import media_source_lines
from clio_agent.gact.skills import SkillRef

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_capability_selection import CatalogSelection
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry

#: Reads the session's current catalog selection (``select_catalog(record=False)``).
SelectionReader = Callable[[], "CatalogSelection"]

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


def _index_line(name: str, description: str, signature: str = "") -> str:
    parts = [f"- `{name}`"]
    if description:
        parts.append(f"— {description}")
    if signature:
        parts.append(f"`{signature}`")
    return " ".join(parts) if len(parts) > 1 else parts[0]


def _component_lines(file: dict[str, Any]) -> list[str]:
    """One line per component: its ``description``, then a GENERATED signature.

    The signature (:func:`~clio_agent.gact.a2ui_catalogs.component_signature.
    generate_component_signature`) is read off the component's own JSON
    Schema every render -- never hand-typed, so it can never drift from what
    the validator actually enforces (#1533 phase 3).
    """

    components = file.get("components")
    if not isinstance(components, dict) or not components:
        return []
    lines = ["", "## Components"]
    for name in sorted(components):
        schema = components[name]
        description = str(schema.get("description") or "") if isinstance(schema, dict) else ""
        signature = generate_component_signature(schema)
        lines.append(_index_line(name, description, signature))
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


#: Generated SKILL.md bodies, cached by the catalog FILE's own content
#: checksum (``CatalogEntry.checksum`` -- the same key ``registry.py``'s
#: ``compiled_validators``/``_VALIDATOR_CACHE`` already uses, same doctrine:
#: a cache accelerates, is keyed by content so it can never serve a stale
#: body for a changed catalog, and is invalidated implicitly -- a changed
#: catalog file produces a different checksum, a different cache slot, never
#: a stale hit). Perf finding (CI investigation, PR #1375): every
#: ``skill_runtime_for_agent`` call unconditionally computes BOTH tiers
#: (``skill_bodies_context`` reads every resolved skill's body even for a
#: react expert that only ever uses ``prompt_block``), so an auto-declared
#: catalog skill regenerated its body on EVERY turn build before this cache
#: existed -- measured 40 regenerations across 20 builds (2 catalogs x 20).
_BODY_CACHE: dict[tuple[str, str], str] = {}
_BODY_CACHE_LOCK = threading.Lock()


def generate_catalog_skill_body(
    entry: "CatalogEntry",
    *,
    is_default: bool = False,
    selection: SelectionReader | None = None,
) -> str:
    """Render (or return the cached) generated ``SKILL.md`` text.

    Frontmatter (``name``/``description`` from the catalog file's own
    ``title``/``description``), then the catalog's own ``catalogId`` and
    what a surface created in this session gets: with ``selection`` (a
    session's live :func:`~clio_agent.gact.a2ui_capability_selection.
    select_catalog` read, taken when the skill is loaded) the role states the
    catalog an empty ``catalog_id`` selects and whether this session's client
    renders this one; without it, the agent's declared order (``is_default``:
    its first declared catalog). Then schema lookup guidance and the generated
    function/component index, then the sidecar ``instructions.md`` verbatim
    and declared events. The bounded tool preview must expose exact lookup
    paths before long producer guidance -- never the
    component shapes themselves (the catalog file is the allowlist, S2).
    Cached by ``(checksum, role)``.
    """

    role = _catalog_role(entry, is_default=is_default, selection=selection() if selection else None)
    key = (entry.checksum, role)
    with _BODY_CACHE_LOCK:
        cached = _BODY_CACHE.get(key)
    if cached is not None:
        return cached
    rendered = _render_catalog_skill_body(entry, role=role)
    with _BODY_CACHE_LOCK:
        return _BODY_CACHE.setdefault(key, rendered)


def _catalog_role(
    entry: "CatalogEntry", *, is_default: bool, selection: "CatalogSelection | None"
) -> str:
    """What creating a surface against this catalog does (generated, never hand-typed)."""

    catalog_id = entry.catalog_id
    if selection is None:
        if is_default:
            return (
                "This is this agent's default catalog: a surface created with an empty "
                "`catalog_id` uses it."
            )
        return (
            "This is not this agent's default catalog: a surface created with an "
            f'empty `catalog_id` does not use it, so pass `catalog_id="{catalog_id}"`.'
        )
    if selection.reason == "a2ui_client_capabilities_unknown":
        return (
            "This session's client has not said which catalogs it renders, so no "
            "surface can be created yet."
        )
    if catalog_id not in selection.client_supported_catalog_ids:
        chosen = (
            f" A surface created with an empty `catalog_id` uses `{selection.catalog_id}`."
            if selection.ok
            else ""
        )
        return (
            "This session's client does not render this catalog, so a surface "
            f"cannot use it.{chosen}"
        )
    if selection.catalog_id == catalog_id:
        return (
            "This is the catalog a surface created with an empty `catalog_id` uses in this session."
        )
    return (
        "A surface created with an empty `catalog_id` uses "
        f'`{selection.catalog_id}`, so pass `catalog_id="{catalog_id}"` to use this one.'
    )


def _render_catalog_skill_body(entry: "CatalogEntry", *, role: str) -> str:
    """The actual, uncached rendering (see :func:`generate_catalog_skill_body`)."""

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
        f"Catalog id: `{entry.catalog_id}`",
        "",
        role,
        "",
    ]
    lines.extend(
        [
            "",
            "## Loading component schemas",
            (
                f'Call `load_skill("{skill_id}", '
                'file="catalog.json#/components/<Name>")` for the exact schema '
                "of one component when its signature is insufficient. For several "
                'components, use ONE call with files=["catalog.json#/components/<Name>", '
                "...] and omit file. Include the layout container and any other "
                "components whose property shapes you need; do not guess optional "
                "property values. Include needed action functions in that same batch "
                'with "catalog.json#/functions/<Name>" from the function index. '
                "Each returned schema includes its referenced $defs; do not invent "
                "additional definition paths."
            ),
        ]
    )
    lines.extend(_function_lines(file))
    lines.extend(_component_lines(file))
    lines.extend(["", entry.instructions.strip()])
    lines.extend(_event_lines(entry))
    lines.extend(media_source_lines(file))
    return "\n".join(lines).strip() + "\n"


def _catalog_skill_ref(
    entry: "CatalogEntry",
    *,
    is_default: bool = False,
    selection: SelectionReader | None = None,
) -> SkillRef:
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
        body_provider=functools.partial(
            generate_catalog_skill_body, entry, is_default=is_default, selection=selection
        ),
    )


def discover_catalog_skill_refs(
    entries: "list[CatalogEntry]", *, selection: SelectionReader | None = None
) -> list[SkillRef]:
    """Return one generated :class:`SkillRef` per given catalog entry.

    The CALLER resolves which entries are in scope
    (:func:`clio_agent.gact.skills.SkillCatalog._catalog_refs` passes this
    session's resolved catalogs -- the agent's own declared allowlist, via
    :func:`~clio_agent.gact.a2ui_catalogs.activation.
    resolve_session_catalogs`) -- this function only ever RENDERS entries
    into skill refs (the FIRST entry is the agent's default catalog, since the
    resolution is in declared preference order), never decides producibility (a bare
    ``registry.installed()`` walk would miss a path-activated pack the
    app-level registry's own discovery never sees, silently under-declaring
    catalog skills for exactly that session). ``selection`` reads the session's
    live catalog selection when a skill body is loaded, so each body states what
    an empty or explicit ``catalog_id`` actually gets with this client.
    """

    return [
        _catalog_skill_ref(entry, is_default=index == 0, selection=selection)
        for index, entry in enumerate(entries)
    ]


__all__ = [
    "CATALOG_SKILL_PREFIX",
    "catalog_skill_id",
    "discover_catalog_skill_refs",
    "generate_catalog_skill_body",
]
