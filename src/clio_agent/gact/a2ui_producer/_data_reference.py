"""Producer-side validation of ``dataUri``-referenced datasets (clio-agent#1533 S4).

The data-by-reference catalog contract (clio-schemas PR #17) lets map,
data-table, workflow, code, mermaid, diff and chart components carry a
``dataUri`` (``artifact://artifact_<id>``) instead of inline values. The
catalog's JSON Schema can only check the *shape* of that string (the
``artifact://...`` pattern); it cannot see whether the artifact exists, is
readable, or actually carries the columns a ``*Field`` property names. This
module is that second, dynamic check — run once, right after the export
boundary (:mod:`clio_agent.gact.a2ui_producer._export`) and before the batch
is applied, so a broken reference is a typed refusal instead of a surface
that renders empty or errors client-side.

Three checks, by component:

* **Every ``dataUri``** must resolve to a registered, readable artifact
  (``a2ui_data_reference_not_found`` / ``a2ui_data_reference_unreadable``).
* **map / data-table / chart** ``*Field`` properties (the dataset column
  names, including ``selectionField`` when a component binds ``selection``)
  are checked against the referenced CSV/Parquet artifact's real schema via
  the same reader the table-query engine uses
  (:mod:`clio_agent.gact.artifacts.table_query`) — a name not in the dataset
  is ``a2ui_field_not_in_dataset``, naming the available columns. A
  ``dataUri`` naming a non-tabular artifact is
  ``a2ui_data_reference_unsupported_format``.
* **workflow** ``dataUri`` content must be a JSON object shaped
  ``{"nodes": [...], "edges": [...]}``; **code / mermaid / diff** ``dataUri``
  content must be readable as UTF-8 text (it becomes the component's literal
  value). Either failing is ``a2ui_data_reference_shape_invalid`` /
  ``a2ui_data_reference_unreadable``.

A component whose ``dataUri`` does not even match the ``artifact://...``
pattern is left untouched here — the catalog's own schema validation reports
that as ``a2ui_validation_failed``, so this module never duplicates it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_producer._refusal import refusal

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactRecord

#: ``*Field`` properties naming a dataset column, per component. Every
#: tabular component that binds ``selection`` also carries ``selectionField``
#: (the column its shared ``SelectionState`` keys on) -- validated against
#: the dataset's real columns exactly like every other ``*Field``.
_TABULAR_FIELD_PROPERTIES: dict[str, tuple[str, ...]] = {
    "clio.map.v1": (
        "latitudeField",
        "longitudeField",
        "labelField",
        "idField",
        "detailField",
        "categoryField",
        "selectionField",
    ),
    "clio.data-table.v1": ("selectionField",),
    "clio.chart.v1": (
        "xField",
        "yField",
        "entityField",
        "colorField",
        "facetField",
        "selectionField",
    ),
}
#: Tabular components whose ``dataUri`` is existence/readability checked even
#: with no ``*Field`` properties of their own (``clio.data-table.v1``).
_TABULAR_COMPONENTS = frozenset({"clio.map.v1", "clio.data-table.v1", "clio.chart.v1"})
#: dataUri's file content IS the component's literal text value.
_TEXT_COMPONENTS = frozenset({"clio.code.v1", "clio.mermaid.v1", "clio.diff.v1"})
_WORKFLOW_COMPONENT = "clio.workflow.v1"

_DATA_URI_RE = re.compile(r"^artifact://(artifact_[A-Za-z0-9_-]+)$")


def _artifact_id_from_data_uri(value: str) -> str | None:
    match = _DATA_URI_RE.match(value)
    return match.group(1) if match else None


def _resolve_artifact_path(
    app: "FastAPI", artifact_id: str
) -> "tuple[ArtifactRecord, Path] | dict[str, Any]":
    """Resolve ``artifact_id`` to its record plus verified byte path, or a refusal."""

    from fastapi import HTTPException  # noqa: PLC0415

    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    found = get_registry(app).get_by_artifact_id(artifact_id)
    if found is None:
        return refusal(
            "a2ui_data_reference_not_found",
            detail=f"referenced artifact is not registered: {artifact_id}",
        )
    record, version = found
    try:
        # Reused verbatim (owned-store first, then CAS/path, integrity-checked) --
        # the SAME resolution the table-query route runs, format-agnostic.
        from clio_agent.gact.routes.artifact_table_query import _table_source  # noqa: PLC0415

        path = _table_source(app, record, version)
    except HTTPException as exc:
        detail_obj = exc.detail if isinstance(exc.detail, dict) else {}
        message = detail_obj.get("error", {}).get("message") or str(exc.detail)
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"artifact {artifact_id} is not retrievable: {message}",
        )
    return record, path


def _validate_tabular_fields(
    record: "ArtifactRecord",
    path: Path,
    *,
    component_id: str,
    wanted_fields: dict[str, str],
) -> dict[str, Any] | None:
    from clio_agent.gact.artifacts.table_query_models import (  # noqa: PLC0415
        TableQueryError,
        table_format_for,
    )

    fmt = table_format_for(record.name)
    if fmt is None:
        return refusal(
            "a2ui_data_reference_unsupported_format",
            detail=(
                f"component {component_id!r} dataUri names {record.name!r}, which is "
                "not a registered CSV or Parquet table; *Field names cannot be "
                "validated against it"
            ),
        )
    from clio_agent.gact.artifacts.table_query import _read_schema  # noqa: PLC0415

    try:
        schema = _read_schema(path, fmt)
    except TableQueryError as exc:
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"component {component_id!r} dataset could not be read: {exc.message}",
        )
    available = list(schema.names)
    missing = {prop: value for prop, value in wanted_fields.items() if value not in available}
    if missing:
        named = ", ".join(f"{prop}={value!r}" for prop, value in sorted(missing.items()))
        return refusal(
            "a2ui_field_not_in_dataset",
            detail=(
                f"component {component_id!r} names a column absent from "
                f"{record.name!r}: {named}; available columns: {available}"
            ),
        )
    return None


def _validate_workflow_shape(path: Path, *, component_id: str) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"component {component_id!r} dataUri file could not be read: {exc}",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return refusal(
            "a2ui_data_reference_shape_invalid",
            detail=f"component {component_id!r} dataUri is not valid JSON: {exc}",
        )
    if not (
        isinstance(parsed, dict)
        and isinstance(parsed.get("nodes"), list)
        and isinstance(parsed.get("edges"), list)
    ):
        return refusal(
            "a2ui_data_reference_shape_invalid",
            detail=(
                f"component {component_id!r} dataUri JSON must be shaped "
                '{"nodes": [...], "edges": [...]}'
            ),
        )
    return None


def _validate_text_readable(path: Path, *, component_id: str) -> dict[str, Any] | None:
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"component {component_id!r} dataUri file could not be read as text: {exc}",
        )
    return None


def validate_component_data_references(
    app: "FastAPI", components: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Validate every ``dataUri``-carrying component's referenced dataset.

    Args:
        app: The GACT app (the artifact registry lives on ``app.state``).
        components: The producer call's components, after the export boundary
            has already turned workspace paths into ``artifact://`` refs.

    Returns:
        ``None`` when every ``dataUri`` reference (if any) resolves and, for
        map/chart, its ``*Field`` names are real dataset columns; otherwise a
        typed refusal dict (see :func:`clio_agent.gact.a2ui_producer._refusal.
        refusal`), never raised.
    """

    for component in components:
        if not isinstance(component, dict):
            continue
        name = component.get("component")
        data_uri = component.get("dataUri")
        if not isinstance(name, str) or not isinstance(data_uri, str) or not data_uri:
            continue
        artifact_id = _artifact_id_from_data_uri(data_uri)
        if artifact_id is None:
            # Malformed against the component's own dataUri pattern -- the
            # catalog's schema validator reports this, never duplicated here.
            continue
        component_id = str(component.get("id") or "")
        resolved = _resolve_artifact_path(app, artifact_id)
        if isinstance(resolved, dict):
            return resolved
        record, path = resolved

        if name in _TABULAR_COMPONENTS:
            field_props = _TABULAR_FIELD_PROPERTIES.get(name, ())
            wanted_fields = {
                prop: component[prop]
                for prop in field_props
                if isinstance(component.get(prop), str) and component[prop]
            }
            if wanted_fields:
                outcome = _validate_tabular_fields(
                    record, path, component_id=component_id, wanted_fields=wanted_fields
                )
                if outcome is not None:
                    return outcome
        elif name == _WORKFLOW_COMPONENT:
            outcome = _validate_workflow_shape(path, component_id=component_id)
            if outcome is not None:
                return outcome
        elif name in _TEXT_COMPONENTS:
            outcome = _validate_text_readable(path, component_id=component_id)
            if outcome is not None:
                return outcome
    return None


__all__ = ["validate_component_data_references"]
