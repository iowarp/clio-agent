"""A JSON value an agent sent as its text, decoded for a typed tool argument.

Models sometimes send a structured or scalar argument as its JSON text
(``options='{"max_rounds": 0}'``, ``limit="15"``, ``exact="true"``). A call written as
invoke markup carries every value as text, and one whose tool's schema the parser could
not find reaches the tool that way: every researcher's ``literature_search(limit="15")``
was refused with "Arg limit is invalid: '15' is not of type 'integer'" (2026-11-17).

When an argument does not take text and the text is exactly one JSON value of a type it
does take, the value is that; nothing else changes, and each decode is logged
(``tool_argument_json_text_decoded``). Any other text is left for validation to refuse.
Both CLIO's native tools and bridged MCP tools use it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def schema_types(schema: dict[str, Any] | None) -> list[str]:
    """The JSON types an argument's schema accepts (``anyOf``/``oneOf`` included)."""

    schema = schema or {}
    kind = schema.get("type")
    if isinstance(kind, str):
        return [kind]
    if isinstance(kind, list):
        return [str(item) for item in kind]
    types: list[str] = []
    for option in schema.get("anyOf") or schema.get("oneOf") or []:
        for item in schema_types(option if isinstance(option, dict) else None):
            if item not in types:
                types.append(item)
    return types


#: The Python types a JSON-schema type admits for :func:`_decoded` (``bool`` is not a number).
_DECODED_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def _decoded(value: Any, schema: dict[str, Any] | None) -> Any:
    """A JSON value given as its text, decoded: a format fix only.

    Models sometimes send a structured or scalar argument as its JSON text
    (``options='{"max_rounds": 0}'``, ``limit="15"``, ``exact="true"``). When the
    argument does not take text and the text is exactly one JSON value of a type it
    does take, the value is that; nothing else changes. Any other text is left for
    validation to refuse.
    """

    if not isinstance(value, str):
        return value
    types = schema_types(schema)
    if "string" in types or not set(_DECODED_TYPES) & set(types):
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    for kind in types:
        admitted = _DECODED_TYPES.get(kind, ())
        if isinstance(decoded, admitted) and not (
            isinstance(decoded, bool) and kind in {"integer", "number"}
        ):
            return decoded
    return value


def decode_typed_text_arguments(
    tool: str, arguments: Mapping[str, Any], schemas: Mapping[str, Any]
) -> dict[str, Any]:
    """``arguments`` with each JSON text decoded where its schema asks for that type."""

    decoded_arguments = dict(arguments)
    for key, value in arguments.items():
        schema = schemas.get(key)
        decoded = _decoded(value, schema if isinstance(schema, dict) else None)
        if decoded is not value:
            logger.info(
                "tool_argument_json_text_decoded tool=%s argument=%s "
                "reason=json_text_for_typed_argument",
                tool,
                key,
            )
            decoded_arguments[key] = decoded
    return decoded_arguments


__all__ = ["decode_typed_text_arguments", "schema_types"]
