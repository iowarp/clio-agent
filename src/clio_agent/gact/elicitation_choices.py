"""What each option of an elicitation's choice means, and which one is recommended.

A pure helper of :mod:`clio_agent.gact.elicitation_schema`. Any MCP server's form
can say both: a titled option's ``description`` is its meaning and a single
select's ``default`` its recommendation. A server may also send a typed copy
under ``_meta`` (clio-relay's ``io.iowarp.clio-relay/choice``); it fills an
option the schema leaves undescribed and names the recommendation explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from clio_agent.tools.mcp_extension_registry import RELAY_CHOICE_EXTENSION_ID

__all__ = ["annotate_choices"]


def _typed_choice(meta: Mapping[str, Any] | None, field_name: str) -> Mapping[str, Any] | None:
    """The typed choice a server sent for ``field_name`` under its ``_meta``, if any.

    clio-relay keys ``_meta["io.iowarp.clio-relay/choice"]`` by ask id, then field;
    an entry keyed by field directly is accepted too. Each entry holds ``options``
    (``value``, ``label``, ``description``, ``recommended``) and ``recommended``.
    """

    by_ask = (meta or {}).get(RELAY_CHOICE_EXTENSION_ID)
    if not isinstance(by_ask, Mapping):
        return None
    for scope in (by_ask, *by_ask.values()):
        entry = scope.get(field_name) if isinstance(scope, Mapping) else None
        if isinstance(entry, Mapping) and isinstance(entry.get("options"), Sequence):
            return entry
    return None


def _meanings(field_: Mapping[str, Any], typed: Mapping[str, Any] | None) -> tuple[list[str], Any]:
    """Each option's meaning and the recommended value of one choice field."""

    values = [str(value) for value in field_.get("enum") or []]
    meanings = list(field_.get("enum_descriptions") or [""] * len(values))
    recommended = field_.get("default")
    if typed is not None:
        by_value: dict[str, Mapping[str, Any]] = {
            str(item.get("value")): item
            for item in typed.get("options") or []
            if isinstance(item, Mapping)
        }
        for index, value in enumerate(values):
            found = by_value.get(value) or {}
            if not meanings[index] and isinstance(found.get("description"), str):
                meanings[index] = found["description"]
        flagged = next((v for v in values if (by_value.get(v) or {}).get("recommended")), None)
        named = typed.get("recommended")
        if named is not None and str(named) in values:
            recommended = named
        elif flagged is not None:
            recommended = flagged
    if recommended is None or isinstance(recommended, list) or str(recommended) not in values:
        recommended = None
    return meanings, recommended


def annotate_choices(fields: Sequence[dict[str, Any]], meta: Mapping[str, Any] | None) -> None:
    """Set every choice field's ``enum_descriptions`` and ``recommended`` in place.

    A multi-select's ``default`` is a preselection, not one recommended option, so
    only a single select gets ``recommended``.
    """

    for built in fields:
        if not built.get("enum"):
            continue
        meanings, recommended = _meanings(built, _typed_choice(meta, str(built["name"])))
        built["enum_descriptions"] = meanings if any(meanings) else None
        built["recommended"] = None if built.get("multi") else recommended
