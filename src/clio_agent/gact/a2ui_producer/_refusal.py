"""Typed producer-tool refusals — TOOL RESULTS the model can read (S4).

A producer mistake (unknown catalog, failing component schema, no client
catalog selectable, a settled transcript) is returned as
``{"ok": False, "reason": <code>, "detail": ..., "hint": ...}``, never raised
— the model corrects course from the result, the same way it reads any other
tool observation (⚑ #1: clio surfaces reality, it does not decide FOR the
model by throwing).
"""

from __future__ import annotations

import re
from typing import Any

#: Matches the ``component=<Name>`` prefix ``validate_components`` stamps on
#: a schema-failure message (``gact/a2ui_catalogs/validation.py``).
_COMPONENT_EQ_RE = re.compile(r"component=(\S+)")
#: Matches "A2UI component is not in catalog <id>: <Name>" (an unimplemented
#: component name, same module).
_NOT_IN_CATALOG_RE = re.compile(r"A2UI component is not in catalog [^:]+: (\S+)$")


def refusal(reason: str, *, detail: str, hint: str = "") -> dict[str, Any]:
    """Build a typed producer-tool refusal dict (never raised as an exception)."""

    return {"ok": False, "reason": reason, "detail": detail, "hint": hint}


def component_from_validation_error(message: str) -> str:
    """Extract the failing component's name from an S2 validation message, or ``""``."""

    match = _COMPONENT_EQ_RE.search(message)
    if match:
        return match.group(1)
    match = _NOT_IN_CATALOG_RE.search(message)
    return match.group(1) if match else ""


def catalog_hint(app: Any, catalog_id: str) -> str:
    """The exact ``load_skill(...)`` call for a catalog's own guidance, or ``""``."""

    from clio_agent.gact.a2ui_catalogs.skills import catalog_skill_id  # noqa: PLC0415

    registry = getattr(app.state, "a2ui_catalogs", None)
    entry = registry.get(catalog_id) if registry is not None and catalog_id else None
    if entry is None:
        return ""
    return f'load_skill("{catalog_skill_id(entry)}")'


def component_hint(app: Any, catalog_id: str, message: str) -> str:
    """The exact ``load_skill(...)`` call for the failing component's schema.

    Falls back to the catalog-level hint when the failing component's name
    cannot be parsed out of ``message``, and to ``""`` when the catalog
    itself cannot be resolved.
    """

    from clio_agent.gact.a2ui_catalogs.skills import catalog_skill_id  # noqa: PLC0415

    registry = getattr(app.state, "a2ui_catalogs", None)
    entry = registry.get(catalog_id) if registry is not None and catalog_id else None
    if entry is None:
        return ""
    skill_id = catalog_skill_id(entry)
    component = component_from_validation_error(message)
    if component:
        return f'load_skill("{skill_id}", file="catalog.json#/components/{component}")'
    return f'load_skill("{skill_id}")'


__all__ = ["catalog_hint", "component_from_validation_error", "component_hint", "refusal"]
