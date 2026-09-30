"""JSON Pointer fragment resolution for ``load_skill`` (#916 S3, #1533 S4).

Split out of :mod:`clio_agent.gact.agents.skill_runtime` (no-accretion ground
rule): resolving ``load_skill(skill_id, file="catalog.json#/components/X")``
is a self-contained concern (parse the RFC 6901 pointer, inline the
document's own ``$defs``, name what is left) with no dependency on the rest
of the skill runtime (agent declarations, prompt blocks, tool building).

Inlining every local ``#/$defs/<Name>`` reference reachable under the
resolved node means one ``load_skill`` call fully explains a catalog
component — e.g. ``clio.map.v1``'s ``MapPoint``/``DataQuery``/
``CatalogComponentCommon`` shapes are expanded in place, not left as bare
refs needing a second lookup. An external (e.g. ``a2ui.org``
``common_types.json``) reference is left as a ``$ref`` and only named in a
trailing line, since this call cannot load it. A genuinely recursive
definition (the chart spec guard's ``SpecNoForbiddenKeys``, which ``$ref``s
itself to walk a Vega-Lite spec at any depth) is detected by CYCLE, not by
an arbitrary depth number: :func:`_inline_local_defs` tracks the chain of def
names currently being expanded, and a ``$ref`` back to one of them is left
in place, annotated, instead of expanded forever.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: A local ``$defs`` reference this document defines itself, e.g.
#: ``#/$defs/FieldName`` -- the shape :func:`_inline_local_defs` expands.
_LOCAL_DEF_REF_RE = re.compile(r"^#/\$defs/([^/]+)$")


def _collect_ref_targets(node: Any) -> set[str]:
    """Return every ``$ref`` string reachable under ``node`` (any nesting)."""

    found: set[str] = set()
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref)
        for value in node.values():
            found.update(_collect_ref_targets(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_collect_ref_targets(item))
    return found


def _inline_local_defs(document: Any, node: Any, *, expanding: frozenset[str] = frozenset()) -> Any:
    """Recursively inline every ``#/$defs/<Name>`` ref reachable under ``node``.

    ``document`` is the whole parsed JSON file (its top-level ``$defs`` is the
    lookup table); ``node`` is the (sub)value being expanded. A sibling key
    next to ``$ref`` (e.g. a property's own ``description`` overriding the
    def's) is preserved and wins over the inlined def's own value for that
    key.

    No depth counter: an arbitrary number is either too tight (a legitimate
    schema nests deeper than guessed — the original bug here) or too loose to
    mean anything. The only real hazard is a CYCLE (a genuinely recursive
    definition, e.g. the chart spec guard's ``SpecNoForbiddenKeys``, which
    ``$ref``s itself to walk a Vega-Lite spec at any depth) — ``expanding``
    tracks the chain of def names currently being expanded, and a ``$ref``
    back to one of them is left in place (with a ``$comment`` naming it as
    recursive) instead of looping forever. Every non-cyclic ``$ref``, at any
    nesting, inlines fully.
    """

    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            match = _LOCAL_DEF_REF_RE.match(ref)
            if match:
                def_name = match.group(1)
                if def_name in expanding:
                    # Genuinely recursive (e.g. SpecNoForbiddenKeys $ref-ing
                    # itself) -- expanding it further would never terminate.
                    # Leave THIS $ref in place, annotated, rather than loop.
                    recursive = dict(node)
                    recursive["$comment"] = (
                        f"recursive: #/$defs/{def_name} is already being expanded on "
                        "this path, left as a $ref"
                    )
                    return recursive
                defs = document.get("$defs") if isinstance(document, Mapping) else None
                target = defs.get(def_name) if isinstance(defs, Mapping) else None
                if target is not None:
                    expanded = _inline_local_defs(
                        document, target, expanding=expanding | {def_name}
                    )
                    merged: dict[str, Any] = dict(expanded) if isinstance(expanded, Mapping) else {}
                    for key, value in node.items():
                        if key != "$ref":
                            merged[key] = value
                    return merged
        return {
            key: _inline_local_defs(document, value, expanding=expanding)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_inline_local_defs(document, item, expanding=expanding) for item in node]
    return node


def resolve_json_pointer_fragment(file_path: str, raw_text: str, fragment: str) -> str:
    """Resolve an RFC 6901 JSON Pointer ``fragment`` against a bundled JSON file.

    Every local ``#/$defs/<Name>`` reference reachable under the resolved
    node is inlined (:func:`_inline_local_defs`) — e.g. a component's own
    ``MapPoint``/``DataTableColumn``/``DataQuery``/``CatalogComponentCommon``
    shape is expanded in place, so one load fully explains it. An external
    (e.g. ``common_types.json``) reference is left as a ``$ref`` and named in
    one trailing line, since this call cannot load it.

    Raises:
        ValueError: ``file_path`` does not end in ``.json`` (fragments are
            JSON-only, never silently ignored); ``fragment`` is not an
            absolute pointer (does not start with ``/``); or the pointer does
            not resolve — the message names the available keys at the
            nearest resolvable parent.
    """

    if not file_path.lower().endswith(".json"):
        raise ValueError(
            f"file {file_path!r} does not support a '#' fragment: JSON pointer "
            "fragments are only supported for .json bundled files"
        )
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bundled file {file_path!r} is not valid JSON: {exc}") from exc
    if not fragment.startswith("/"):
        raise ValueError(f"fragment {fragment!r} must be an absolute JSON pointer (start with '/')")
    node: Any = document
    walked: list[str] = []
    for raw_part in fragment.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and part in node:
            node = node[part]
            walked.append(part)
            continue
        if isinstance(node, list):
            index = int(part) if part.isdigit() else -1
            if 0 <= index < len(node):
                node = node[index]
                walked.append(part)
                continue
        if isinstance(node, Mapping):
            available: list[str] = sorted(node.keys())
        elif isinstance(node, list):
            available = [str(i) for i in range(len(node))]
        else:
            available = []
        pointer_so_far = "/" + "/".join(walked)
        raise ValueError(
            f"JSON pointer {fragment!r} does not resolve in {file_path!r}: no "
            f"{part!r} at {pointer_so_far!r}; available keys: {available}"
        )
    inlined = _inline_local_defs(document, node)
    rendered = json.dumps(inlined, indent=2, sort_keys=False)
    refs = sorted(_collect_ref_targets(inlined))
    # A local ref (bare "#/...") that inlining did not expand (e.g. it points
    # somewhere other than "#/$defs/<Name>", or a recursive one left in place
    # above) is still itself loadable with another
    # load_skill(..., file="catalog.json#/...") call; a ref into an external
    # file (typically common_types.json) names a STANDARD shape this catalog
    # does not define and this call cannot load.
    local_refs = sorted(f"catalog.json{ref}" for ref in refs if ref.startswith("#/"))
    standard_refs = sorted(ref for ref in refs if not ref.startswith("#/"))
    if local_refs:
        rendered += "\n\nLocal refs still needing a separate load_skill(..., file=): " + ", ".join(
            local_refs
        )
    if standard_refs:
        rendered += "\n\nStandard refs (not loadable here): " + ", ".join(standard_refs)
    return rendered


__all__ = ["resolve_json_pointer_fragment"]
