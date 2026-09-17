"""Model-facing presentation for producer-tool results (S4).

Surface kind labels are DERIVED from the resolved catalog entry, never a
hand-maintained table (adversarial-review fix, S4): a component is "Input"
when its OWN schema composes the official ``Checkable`` mixin
(``common_types.json#/$defs/Checkable``, the protocol's own marker for a
component that supports client-side ``checks``), and its label otherwise
comes from its RESOLVED KERNEL name (the sidecar's
``implements[<name>].kernel``, falling back to the bare name when
unaliased), parsed the same ``clio.<kind>.v<n>`` way as before -- so a pack
catalog that aliases a kernel under its own vocabulary still labels
correctly without this module knowing the pack's names in advance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_catalogs.registry import CatalogEntry


def _label_from_component_name(name: str) -> str:
    """Derive a human-facing kind label from a ``clio.<kind>.v<n>`` component name."""

    if not name.startswith("clio."):
        return "Interface"
    middle = name[len("clio.") :]
    if middle.endswith(".v1"):
        middle = middle[: -len(".v1")]
    words = [part for part in middle.replace("-", " ").replace(".", " ").split() if part]
    return " ".join(word.capitalize() for word in words) or "Interface"


def _is_checkable(entry: "CatalogEntry", name: str) -> bool:
    """Whether ``name``'s own catalog-file schema composes the Checkable mixin."""

    components = entry.file.get("components")
    definition = components.get(name) if isinstance(components, Mapping) else None
    if not isinstance(definition, Mapping):
        return False
    for member in definition.get("allOf", []) or []:
        if isinstance(member, Mapping):
            ref = member.get("$ref")
            if isinstance(ref, str) and ref.rstrip("/").endswith("/Checkable"):
                return True
    return False


def _kernel_name(entry: "CatalogEntry", name: str) -> str:
    """The renderer kernel ``name`` resolves to (a pack alias's real identity)."""

    implementation = entry.sidecar.implements.get(name)
    return implementation.kernel if implementation is not None else name


def _resolve_catalog_entry(row: Mapping[str, Any]) -> "CatalogEntry":
    """Best-effort catalog entry for kind derivation.

    Prefers the session's own resolved catalog (``row["catalog_id"]`` plus
    the live app's registry); falls back to the builtin CLIO workspace
    catalog when neither is available (a refusal before catalog resolution,
    or a caller with no active session) so kind labeling degrades to a
    reasonable default rather than losing classification entirely.
    """

    catalog_id = str(row.get("catalog_id") or "")
    app = _ctx.active_app()
    if app is not None and catalog_id:
        registry = getattr(getattr(app, "state", None), "a2ui_catalogs", None)
        if registry is not None:
            entry = registry.get(catalog_id)
            if entry is not None:
                return entry
    from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs  # noqa: PLC0415

    _, workspace = load_builtin_catalogs()
    return workspace


def _surface_kind(components: Any, row: Mapping[str, Any]) -> str:
    """Return the dominant human-facing kind in an A2UI component array."""

    if not isinstance(components, list):
        return "Interface"
    names = {
        str(component.get("component"))
        for component in components
        if isinstance(component, Mapping) and component.get("component")
    }
    if not names:
        return "Interface"
    entry = _resolve_catalog_entry(row)
    if any(_is_checkable(entry, name) for name in names):
        return "Input"
    for name in sorted(names):
        kernel = _kernel_name(entry, name)
        if kernel.startswith("clio."):
            return _label_from_component_name(kernel)
    for name in sorted(names):
        if _kernel_name(entry, name) == "Text":
            return "Text"
    return "Interface"


def _action_label(row: Mapping[str, Any]) -> str:
    if row.get("deleted"):
        return "Delete UI element"
    if row.get("created") is False:
        return "Update UI element"
    return "Generate UI element"


def surface_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Describe a producer-tool call without exposing its protocol envelope."""

    kwargs = args.get("kwargs")
    call_args: Mapping[str, Any] = kwargs if isinstance(kwargs, Mapping) else args
    payload = structured if isinstance(structured, Mapping) else result
    row = payload if isinstance(payload, Mapping) else {}
    surface_id = str(row.get("surface_id") or call_args.get("surface_id") or "")
    surface_kind = _surface_kind(call_args.get("components"), row)
    failed = row.get("ok") is False or bool(row.get("error")) or row.get("rendered") is False
    blocks: list[dict[str, Any]] = [
        {
            "id": "surface",
            "type": "link",
            "target": "surface",
            "uri": surface_id,
            "label": surface_kind,
        },
    ]
    if failed:
        detail = str(
            row.get("detail") or row.get("message") or row.get("reason") or row.get("error") or ""
        )
        if detail:
            blocks.append(
                {
                    "id": "error",
                    "type": "text",
                    "severity": "error",
                    "text": detail,
                }
            )
    return {
        "action": _action_label(row),
        "subject": "surface",
        "status": "failed" if failed else "succeeded",
        "summary": "",
        "blocks": blocks,
    }


__all__ = ["surface_presentation"]
