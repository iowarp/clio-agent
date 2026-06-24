"""Type / signature-DSL leaf helpers for the GACT server (#714 decomposition).

This module owns the pure, leaf-level helpers that translate a marketplace
blueprint's ``signature`` field-type DSL (e.g. ``region: list[float]``,
``status: Literal["staged","blocked"]``) into real Python / Pydantic annotations
the DSPy adapter can force, parse, and validate -- replacing post-hoc regex
inference. It is deliberately a *leaf*: every function here is pure (no closures,
no ``app.state``, no module globals beyond the static scalar-type table), and it
imports only stdlib + ``pydantic`` (lazily, inside the one function that needs
it). Folding these out before the heavily-coupled expert runtime keeps that later
move free of any ``app.py`` import.

Responsibilities:

* :func:`_parse_field_annotation` -- map a blueprint field-type spec to a real
  annotation (scalars, ``dict``/``object``/``json``, ``list[...]``/``array[...]``,
  ``optional[...]``, ``Literal[...]``, and nested ``object`` with ``fields:``).
* :func:`_sanitize_model_name` -- make a safe Pydantic model class name.
* :func:`_is_optional_annotation` -- detect ``Optional[X]`` / ``X | None``.
* :func:`_blueprint_module_kind` -- the validated ``module.kind`` of a blueprint
  AgentDef (``predict`` / ``chain_of_thought`` / ``react``).
* :data:`_SCALAR_FIELD_TYPES` -- the DSL scalar/collection keyword -> type table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


def _blueprint_module_kind(agent_def: "AgentDef") -> str:
    """Return the validated ``module.kind`` of a blueprint AgentDef.

    One of ``predict`` / ``chain_of_thought`` / ``react`` (defaulting to
    ``predict`` when unset). Raises ``ValueError`` on an unsupported kind -- a
    blueprint authoring error that must fail loud, not silently degrade.
    """
    module = agent_def.module if isinstance(agent_def.module, Mapping) else {}
    kind = str(module.get("kind") or "predict").strip().lower()
    if kind not in {"predict", "chain_of_thought", "react"}:
        raise ValueError(f"unsupported module.kind for {agent_def.id!r}: {kind}")
    return kind


_SCALAR_FIELD_TYPES: dict[str, Any] = {
    "str": str,
    "string": str,
    "text": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "bool": bool,
    "boolean": bool,
    # Bare collections stay UNTYPED (a generic ``array`` may hold numbers, so
    # defaulting to list[str] would mis-coerce). Element typing is opt-in via the
    # ``list[float]`` / ``list[str]`` DSL form.
    "dict": dict,
    "object": dict,
    "json": dict,
    "list": list,
    "array": list,
}


def _is_optional_annotation(annotation: Any) -> bool:
    """True when ``annotation`` is ``Optional[X]`` / ``X | None``."""
    import typing  # noqa: PLC0415

    origin = typing.get_origin(annotation)
    union_types: tuple[Any, ...] = (typing.Union,)
    try:  # py3.10+ ``X | None`` uses types.UnionType
        import types as _types  # noqa: PLC0415

        union_types = (typing.Union, _types.UnionType)
    except Exception:  # noqa: BLE001 - older interpreters
        pass
    return origin in union_types and type(None) in typing.get_args(annotation)


def _parse_field_annotation(spec: Any, *, model_name: str) -> Any:
    """Map a pack ``signature`` field 'type' DSL to a real Python annotation.

    Supports scalars (``str``/``int``/``float``/``bool``), ``dict``/``object``/
    ``json`` (generic JSON object), ``list[...]``/``array[...]``,
    ``optional[...]``, ``Literal[...]`` (quoted or bare members), and a nested
    ``object`` with a ``fields:`` sub-mapping (compiled to a Pydantic model). This
    lets a marketplace expert declare typed outputs (e.g. ``region: list[float]``,
    ``status: Literal["staged","blocked"]``) that the DSPy adapter forces, parses,
    and validates -- replacing post-hoc regex inference.
    """
    # nested object with declared sub-fields -> generated Pydantic model
    if isinstance(spec, Mapping) and isinstance(spec.get("fields"), Mapping):
        from pydantic import BaseModel, create_model  # noqa: PLC0415

        fields: dict[str, Any] = {}
        for fname, fspec in spec["fields"].items():
            ann = _parse_field_annotation(
                fspec if isinstance(fspec, Mapping) else {"type": fspec},
                model_name=f"{model_name}_{fname}",
            )
            # Precedence for a field's default: an EXPLICIT ``default:`` in the
            # blueprint DSL wins (lets an author mark a stage-invariant field --
            # e.g. discovery's ``analysis_ready: false`` -- so a model that drops
            # the boilerplate key gets the correct value instead of crashing the
            # whole delegation on a Pydantic "Field required"). This is NOT clio
            # deciding semantics: the default is declared by the blueprint and is
            # the field's known value at this stage; routing-critical fields (e.g.
            # ``status``) simply omit ``default`` and stay required. Otherwise an
            # Optional[...] field defaults to None; everything else stays required.
            if isinstance(fspec, Mapping) and "default" in fspec:
                default = fspec["default"]
            elif _is_optional_annotation(ann):
                default = None
            else:
                default = ...
            fields[str(fname)] = (ann, default)
        return create_model(_sanitize_model_name(model_name), __base__=BaseModel, **fields)

    raw = spec.get("type") if isinstance(spec, Mapping) else spec
    if raw in (None, ""):
        return str
    text = str(raw).strip()
    low = text.lower()

    match = re.fullmatch(r"optional\[(.+)\]", low)
    if match:
        return Optional[_parse_field_annotation({"type": match.group(1)}, model_name=model_name)]

    match = re.fullmatch(r"(?:list|array)\[(.+)\]", low)
    if match:
        # Runtime type construction from a blueprint string: the inner is a real
        # type at runtime but is statically Any, which mypy cannot use as a
        # subscript. This is intentional dynamic typing, not a defect.
        inner_list = _parse_field_annotation({"type": match.group(1)}, model_name=model_name)
        return list[inner_list]  # type: ignore[valid-type]

    match = re.fullmatch(r"literal\[(.*)\]", text, flags=re.IGNORECASE)
    if match:
        members = re.findall(r"\"([^\"]*)\"|'([^']*)'|([^,\s]+)", match.group(1))
        values = tuple(a or b or c for (a, b, c) in members if (a or b or c))
        if not values:
            raise ValueError(f"empty Literal in signature field type: {text!r}")
        return Literal[values]  # type: ignore[valid-type]

    return _SCALAR_FIELD_TYPES.get(low, str)


def _sanitize_model_name(name: str) -> str:
    """Make a safe Pydantic model class name from an arbitrary field/agent id."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]", "_", str(name)).strip("_") or "Field"
    if cleaned[0].isdigit():
        cleaned = f"F_{cleaned}"
    return f"{cleaned}_model"
