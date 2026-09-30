"""Generated one-line component signatures for the catalog skill index (#1533).

The owner's ruling on the generated catalog index (issue #1533 phase 3): every
component gets its ``description`` plus a compact SIGNATURE -- required vs
optional properties, and "inline value OR dataUri" alternatives shown as
``points[] | dataUri + latitudeField/longitudeField/labelField [idField,
detailField, categoryField] [dataQuery]; title?`` -- generated from the
catalog's own JSON Schema, never hand-written, so it can never drift from
what the validator actually enforces.

Every catalog component shares one ``allOf`` shape: a ``ComponentCommon`` /
``CatalogComponentCommon`` ``$ref`` (skipped here -- common boilerplate, not
this component's own contract), one plain object carrying THIS component's
``properties``/``required`` (and, for a ``dataUri``-dependent property like
``dataQuery``, a ``dependentRequired``), and, for the 7 "data by reference"
components (map, data-table, workflow, code, mermaid, diff, chart), one or
more "exactly one of" ``oneOf`` branches plus an optional ``dataUri``-keyed
``if``/``then`` naming the fields that come with it. This module reads
exactly that structure -- nothing here is a per-component special case.

A conditional keyed on an enum ``const`` (chart's five per-preset field
lists) is too specific for one line, so it is surfaced only as a pointer
("see schema for preset-specific fields"), never spelled out -- that is
exactly the "at most one schema lookup" progressive-disclosure boundary the
index exists to draw.
"""

from __future__ import annotations

from typing import Any

#: A property counts as naming a dataset column when its (possibly
#: outer-annotated) schema ``$ref``s this local ``$defs`` entry.
_FIELD_NAME_DEF_SUFFIX = "/FieldName"


def _merge_object_entries(
    entries: list[Any],
) -> tuple[dict[str, Any], set[str], dict[str, list[str]]]:
    """Merge every inline (non-``$ref``) object entry's own contract.

    A ``$ref`` entry (``ComponentCommon``, ``CatalogComponentCommon``, or any
    other external/local mixin) is skipped: it names a shared shape, not this
    component's own signature.
    """

    properties: dict[str, Any] = {}
    required: set[str] = set()
    dependent: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "$ref" in entry:
            continue
        props = entry.get("properties")
        if isinstance(props, dict):
            properties.update(props)
        req = entry.get("required")
        if isinstance(req, list):
            required.update(str(r) for r in req)
        dep = entry.get("dependentRequired")
        if isinstance(dep, dict):
            for key, needs in dep.items():
                if isinstance(needs, list):
                    dependent[str(key)] = [str(n) for n in needs]
    return properties, required, dependent


def _alternative_groups(entries: list[Any]) -> list[list[list[str]]]:
    """Every "exactly one of" ``oneOf`` -- each entry a list of branches (field lists).

    Only a ``oneOf`` where EVERY branch names its own ``required`` list
    qualifies (the cross-field-XOR pattern every data-by-reference component
    uses); a ``oneOf`` shaped some other way is not this pattern and is
    skipped rather than misrendered.
    """

    groups: list[list[list[str]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        one_of = entry.get("oneOf")
        if not isinstance(one_of, list) or len(one_of) < 2:
            continue
        branches: list[list[str]] = []
        for branch in one_of:
            if isinstance(branch, dict) and isinstance(branch.get("required"), list):
                branches.append([str(field) for field in branch["required"]])
        if len(branches) == len(one_of) and branches:
            groups.append(branches)
    return groups


def _simple_conditional_extra(entries: list[Any]) -> dict[tuple[str, ...], list[str]]:
    """``if: {required: [...]}`` (no ``properties`` const-check) -> ``then.required``.

    This is the "dataUri requires these named fields" pattern (map/data-table).
    A conditional additionally keyed on a ``properties`` const check (chart's
    per-preset conditionals) is a different, enum-shaped pattern -- flagged by
    :func:`_has_const_conditional`, never merged in here.
    """

    extra: dict[tuple[str, ...], list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        condition = entry.get("if")
        consequence = entry.get("then")
        if not (isinstance(condition, dict) and isinstance(consequence, dict)):
            continue
        if "properties" in condition:
            continue
        trigger = condition.get("required")
        needs = consequence.get("required")
        if isinstance(trigger, list) and isinstance(needs, list):
            extra[tuple(str(field) for field in trigger)] = [str(field) for field in needs]
    return extra


def _has_const_conditional(entries: list[Any]) -> bool:
    """Whether an enum-``const``-keyed conditional exists (chart's presets)."""

    for entry in entries:
        condition = entry.get("if") if isinstance(entry, dict) else None
        if isinstance(condition, dict) and "properties" in condition:
            return True
    return False


def _is_array_property(properties: dict[str, Any], name: str) -> bool:
    schema = properties.get(name)
    return isinstance(schema, dict) and schema.get("type") == "array"


def _is_field_name_property(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("$ref") or "").endswith(_FIELD_NAME_DEF_SUFFIX)


def _branch_label(
    properties: dict[str, Any],
    fields: list[str],
    extra_by_trigger: dict[tuple[str, ...], list[str]],
) -> str:
    rendered = [
        f"{field}[]" if _is_array_property(properties, field) else field for field in fields
    ]
    label = "+".join(rendered)
    extra = extra_by_trigger.get(tuple(fields))
    if extra:
        label += " + " + "/".join(extra)
    return label


def generate_component_signature(schema: Any) -> str:
    """Return the generated compact signature for one catalog component schema.

    Args:
        schema: One entry from a catalog file's ``components`` mapping (the
            raw JSON Schema, ``allOf``-composed for every catalog component).

    Returns:
        The one-line signature, e.g. ``"label, value; detail?, trend?, unit?"``
        or ``"points[] | dataUri + latitudeField/longitudeField/labelField
        [categoryField, detailField, idField] [dataQuery]; ...?"``. Empty
        string for a schema this function cannot read (defensive only).
    """

    if not isinstance(schema, dict):
        return ""
    entries = schema.get("allOf")
    if not isinstance(entries, list) or not entries:
        entries = [schema]

    properties, required, dependent = _merge_object_entries(entries)
    alt_groups = _alternative_groups(entries)
    conditional_extra = _simple_conditional_extra(entries)

    alt_fields = {field for group in alt_groups for branch in group for field in branch}
    extra_fields = {field for fields in conditional_extra.values() for field in fields}
    fixed_required = sorted(required - {"component"} - alt_fields - extra_fields)

    segments: list[str] = []
    if fixed_required:
        segments.append(", ".join(fixed_required))
    if alt_groups:
        rendered_groups = [
            " | ".join(_branch_label(properties, branch, conditional_extra) for branch in group)
            for group in alt_groups
        ]
        segments.append(" and ".join(rendered_groups))
    head = ", ".join(segments)

    consumed = {"component", *fixed_required, *alt_fields, *extra_fields}
    field_group = sorted(
        name
        for name, value in properties.items()
        if name not in consumed and name not in dependent and _is_field_name_property(value)
    )
    dependent_group = sorted(name for name in dependent if name not in consumed)
    other = sorted(
        name
        for name in properties
        if name not in consumed and name not in field_group and name not in dependent_group
    )

    brackets = [group for group in (field_group, dependent_group) if group]
    bracket_text = " ".join("[" + ", ".join(group) + "]" for group in brackets)
    if bracket_text:
        head = f"{head} {bracket_text}" if head else bracket_text

    tail_parts = [f"{name}?" for name in other]
    if _has_const_conditional(entries):
        tail_parts.append("see schema for preset-specific fields")
    tail = ", ".join(tail_parts)

    if head and tail:
        return f"{head}; {tail}"
    return head or tail


__all__ = ["generate_component_signature"]
