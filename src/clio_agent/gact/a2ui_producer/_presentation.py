"""Model-facing presentation for producer-tool results (S4).

Surface kind labels are DERIVED from the component array itself (its ``clio.*``
component names ARE catalog vocabulary) rather than a hand-maintained
``{component_name: label}`` table — a catalog-agnostic derivation, since a
pack catalog's scientific components are not enumerable here in advance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_INPUT_COMPONENTS = frozenset(
    {
        "TextField",
        "TextArea",
        "Checkbox",
        "RadioGroup",
        "Select",
        "Slider",
        "DateTimeInput",
    }
)


def _label_from_component_name(name: str) -> str:
    """Derive a human-facing kind label from a ``clio.<kind>.v<n>`` component name."""

    if not name.startswith("clio."):
        return "Interface"
    middle = name[len("clio.") :]
    if middle.endswith(".v1"):
        middle = middle[: -len(".v1")]
    words = [part for part in middle.replace("-", " ").replace(".", " ").split() if part]
    return " ".join(word.capitalize() for word in words) or "Interface"


def _surface_kind(components: Any) -> str:
    """Return the dominant human-facing kind in an A2UI component array."""

    if not isinstance(components, list):
        return "Interface"
    names = {
        str(component.get("component"))
        for component in components
        if isinstance(component, Mapping) and component.get("component")
    }
    if names & _INPUT_COMPONENTS:
        return "Input"
    for name in sorted(names):
        if name.startswith("clio."):
            return _label_from_component_name(name)
    if "Text" in names:
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
    surface_kind = _surface_kind(call_args.get("components"))
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
