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
  are checked against the referenced dataset's REAL columns via the same
  reader the table-query engine uses (:mod:`clio_agent.gact.artifacts.
  table_query`) — a name not in the dataset is ``a2ui_field_not_in_dataset``,
  naming the available columns. A ``dataUri`` naming a non-tabular artifact
  is ``a2ui_data_reference_unsupported_format``. When the component also
  carries a ``dataQuery``, ``*Field``/``sort``/``columns`` names are checked
  against its OUTPUT columns — the source schema unchanged, or, when
  ``dataQuery.aggregate`` is set, ``groupBy`` plus each metric's own
  ``<column>_<fn>`` name (the exact rule :class:`~clio_agent.gact.artifacts.
  table_query_models.TableAggregate` applies) — while ``filter`` columns and
  the aggregate's own ``groupBy``/metric ``column`` are checked against the
  SOURCE schema, matching the engine's filter-then-aggregate pipeline order.
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
        "valueField",
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
_MESH_COMPONENT = "clio.mesh-viewport.v1"
_RASTER_COMPONENT = "clio.raster-viewport.v1"
_MESH_FORMATS = frozenset({"glb", "gltf", "obj", "stl", "ply", "fbx", "3mf", "vtk", "vtp", "drc"})

_DATA_URI_RE = re.compile(r"^artifact://(artifact_[A-Za-z0-9_-]+)$")


def _artifact_id_from_data_uri(value: str) -> str | None:
    match = _DATA_URI_RE.match(value)
    return match.group(1) if match else None


def _validate_mesh_source(
    path: Path, *, component_id: str, format_hint: Any, name: str
) -> dict[str, Any] | None:
    """Check a mesh's declared or recognizable format before a client loads it."""

    try:
        with path.open("rb") as stream:
            head = stream.read(512)
    except OSError as exc:
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"component {component_id!r} meshUri file could not be read: {exc}",
        )
    if not head:
        return refusal(
            "a2ui_mesh_format_invalid",
            detail=f"component {component_id!r} meshUri names an empty file",
        )
    text = head.decode("utf-8", errors="ignore").lstrip()
    detected: str | None = None
    if head.startswith(b"glTF"):
        detected = "glb"
    elif text.startswith("{") and '"asset"' in text:
        detected = "gltf"
    elif text.startswith("ply"):
        detected = "ply"
    elif text.startswith("# vtk DataFile"):
        detected = "vtk"
    elif "<VTKFile" in text:
        detected = "vtp"
    elif text.startswith(("Kaydara FBX Binary", "; FBX")):
        detected = "fbx"
    elif text.startswith("DRACO"):
        detected = "drc"
    elif re.search(r"(?m)^v\s+[-\d]", text):
        detected = "obj"
    elif text.startswith("solid ") and "facet" in text:
        detected = "stl"
    suffix = Path(name).suffix.lower().lstrip(".")
    if not isinstance(format_hint, str):
        format_hint = None
    chosen = format_hint or detected or (suffix if suffix in _MESH_FORMATS else None)
    if chosen not in _MESH_FORMATS:
        return refusal(
            "a2ui_mesh_format_invalid",
            detail=(
                f"component {component_id!r} meshUri names {name!r}, whose format is unknown; "
                "set format to glb, gltf, obj, stl, ply, fbx, 3mf, vtk, vtp, or drc"
            ),
        )
    if detected and format_hint and detected != format_hint:
        return refusal(
            "a2ui_mesh_format_invalid",
            detail=f"component {component_id!r} format={format_hint!r} conflicts with {name!r} ({detected} bytes)",
        )
    if chosen in {"glb", "gltf"}:
        try:
            with path.open("rb") as stream:
                if chosen == "glb":
                    header = stream.read(20)
                    if len(header) != 20 or header[:4] != b"glTF" or header[16:20] != b"JSON":
                        raise ValueError("GLB has no valid JSON chunk")
                    json_size = int.from_bytes(header[12:16], "little")
                    if json_size > 8 * 1024 * 1024:
                        raise ValueError("GLB JSON chunk exceeds the 8 MiB metadata limit")
                    document = json.loads(stream.read(json_size))
                else:
                    if path.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("glTF JSON exceeds the 8 MiB metadata limit")
                    document = json.load(stream)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return refusal(
                "a2ui_mesh_format_invalid",
                detail=f"component {component_id!r} meshUri glTF metadata could not be read: {exc}",
            )
        if not isinstance(document, dict):
            return refusal(
                "a2ui_mesh_format_invalid",
                detail=f"component {component_id!r} meshUri glTF metadata must be an object",
            )
        for section in ("buffers", "images"):
            entries = document.get(section, [])
            if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
                return refusal(
                    "a2ui_mesh_format_invalid",
                    detail=f"component {component_id!r} meshUri glTF {section} must be an array of objects",
                )
            for item in entries:
                if isinstance(item.get("uri"), str) and not item["uri"].startswith("data:"):
                    return refusal(
                        "a2ui_mesh_format_invalid",
                        detail=(
                            f"component {component_id!r} meshUri glTF {section} references external "
                            "content; register a self-contained GLB or embed resources as data URIs"
                        ),
                    )
    return None


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
        # Reused verbatim (owned-store first, then CAS/path) -- the SAME
        # resolution the table-query route runs, format-agnostic. verify=False:
        # this is a producer-side, best-effort shape check (never a serving
        # read), run on every create/update call, so it trusts the artifact
        # record's own recorded metadata instead of re-hashing the whole
        # (possibly large) file every time (#1533 S4 adversarial review item
        # 6) -- the real integrity check still runs when a client actually
        # reads the data through the table-query/table-preview routes.
        from clio_agent.gact.routes.table_route_shared import table_source  # noqa: PLC0415

        path = table_source(app, record, version, verify=False)
    except HTTPException as exc:
        detail_obj: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        message = detail_obj.get("error", {}).get("message") or str(exc.detail)
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"artifact {artifact_id} is not retrievable: {message}",
        )
    return record, path


def _aggregate_output_columns(aggregate_payload: Any) -> list[str] | None:
    """The real OUTPUT columns ``dataQuery.aggregate`` produces, using the
    SAME naming rule the table-query engine applies
    (``TableAggregate``/``TableMetric.output_name``): ``groupBy`` plus each
    metric's own ``<column>_<fn>``.

    Returns ``None`` when ``aggregate_payload`` does not even parse as a
    ``TableAggregate`` shape — the catalog's own schema validation
    (``apply_messages``, downstream of this module) reports a malformed
    ``dataQuery.aggregate``, never duplicated here.
    """

    from pydantic import ValidationError  # noqa: PLC0415

    from clio_agent.gact.artifacts.table_query_models import TableAggregate  # noqa: PLC0415

    if not isinstance(aggregate_payload, dict):
        return None
    try:
        aggregate = TableAggregate.model_validate(aggregate_payload)
    except ValidationError:
        return None
    return [*aggregate.group_by, *(metric.output_name for metric in aggregate.metrics)]


def _data_query_output_columns(data_query: Any, available: list[str]) -> tuple[list[str], bool]:
    """The dataset columns a rendered ``dataQuery`` actually exposes.

    Returns ``(columns, is_aggregated)`` — ``available`` unchanged (source
    schema) when there is no (parseable) ``dataQuery.aggregate``, else the
    aggregate's own output names. Mirrors the exact source-vs-output split
    ``table_query.py``'s engine makes (#1533 S4 adversarial review item 2 —
    ``*Field``/``sort``/``columns`` name OUTPUT columns, never the source
    columns an aggregate consumed).
    """

    if not isinstance(data_query, dict):
        return available, False
    aggregate_output = _aggregate_output_columns(data_query.get("aggregate"))
    if aggregate_output is None:
        return available, False
    return aggregate_output, True


def _data_query_column_refs(data_query: Any, key: str) -> list[str]:
    """Every well-formed ``column`` named under ``dataQuery[key]``.

    A malformed entry (not an object, or a non-string/empty ``column``) is
    silently skipped here — the catalog's own schema validation reports
    that shape problem, never duplicated in this module.
    """

    if not isinstance(data_query, dict):
        return []
    entries = data_query.get(key)
    if not isinstance(entries, list):
        return []
    return [
        entry["column"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("column"), str) and entry["column"]
    ]


def _missing_column_labels(checks: list[tuple[str, str]], available: list[str]) -> list[str]:
    return sorted(f"{label}={column!r}" for label, column in checks if column not in available)


def _validate_tabular_fields(
    record: "ArtifactRecord",
    path: Path,
    *,
    component_id: str,
    wanted_fields: dict[str, str],
    data_query: Any,
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

    # dataQuery.filter and dataQuery.aggregate's own groupBy/metric columns
    # are read from the SOURCE table -- both run BEFORE any output renaming
    # in the engine's filter -> aggregate -> downsample -> sort pipeline.
    source_checks: list[tuple[str, str]] = [
        ("dataQuery.filter[].column", column)
        for column in _data_query_column_refs(data_query, "filter")
    ]
    if isinstance(data_query, dict) and isinstance(data_query.get("aggregate"), dict):
        aggregate_payload = data_query["aggregate"]
        for column in aggregate_payload.get("groupBy") or []:
            if isinstance(column, str) and column:
                source_checks.append(("dataQuery.aggregate.groupBy", column))
        for metric in aggregate_payload.get("metrics") or []:
            if (
                isinstance(metric, dict)
                and isinstance(metric.get("column"), str)
                and metric["column"]
            ):
                source_checks.append(("dataQuery.aggregate.metrics[].column", metric["column"]))
    missing_source = _missing_column_labels(source_checks, available)
    if missing_source:
        return refusal(
            "a2ui_field_not_in_dataset",
            detail=(
                f"component {component_id!r} dataQuery names a source column absent from "
                f"{record.name!r}: {', '.join(missing_source)}; available columns: {available}"
            ),
        )

    # *Field properties, dataQuery.sort and dataQuery.columns all name OUTPUT
    # columns: the source schema unchanged, or the aggregate's own output
    # names when dataQuery.aggregate is set.
    output_columns, is_aggregated = _data_query_output_columns(data_query, available)
    output_checks: list[tuple[str, str]] = list(wanted_fields.items())
    output_checks += [
        ("dataQuery.sort[].column", column)
        for column in _data_query_column_refs(data_query, "sort")
    ]
    if isinstance(data_query, dict):
        for column in data_query.get("columns") or []:
            if isinstance(column, str) and column:
                output_checks.append(("dataQuery.columns[]", column))
    missing_output = _missing_column_labels(output_checks, output_columns)
    if missing_output:
        scope = "the dataQuery.aggregate output" if is_aggregated else repr(record.name)
        return refusal(
            "a2ui_field_not_in_dataset",
            detail=(
                f"component {component_id!r} names a column absent from {scope}: "
                f"{', '.join(missing_output)}; available columns: {output_columns}"
            ),
        )
    return None


#: A per-chunk memory bound for streaming text reads (never a cap on how much
#: of a file gets checked -- every byte is still read and decoded, just not
#: held in memory all at once; #1533 S4 adversarial review item 6).
_STREAM_CHUNK_BYTES = 1 << 20  # 1 MiB


def _read_text_streaming(path: Path) -> str:
    """Read ``path`` as UTF-8 text in bounded chunks, decoding incrementally.

    Raises the same ``(OSError, UnicodeDecodeError)`` a plain
    ``path.read_text()`` would, but never holds more than one chunk plus the
    incremental decoder's small internal buffer at a time while scanning --
    the caller (a shape check on a component's ``dataUri``) still needs the
    full decoded text (JSON parsing has no way around that), but the READ
    itself is bounded, not a single unbounded allocation.
    """

    import codecs  # noqa: PLC0415

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    parts: list[str] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                parts.append(decoder.decode(b"", final=True))
                break
            parts.append(decoder.decode(chunk))
    return "".join(parts)


def _check_text_readable_streaming(path: Path) -> None:
    """Confirm ``path`` decodes as UTF-8, without retaining its content.

    Unlike :func:`_read_text_streaming`, this discards each decoded chunk
    immediately -- a text-component readability check never needs the
    content itself (the export boundary already captured it as the
    component's literal value), so this stays O(chunk size) in memory
    regardless of file size.
    """

    import codecs  # noqa: PLC0415

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                decoder.decode(b"", final=True)
                break
            decoder.decode(chunk)


def _validate_workflow_shape(path: Path, *, component_id: str) -> dict[str, Any] | None:
    try:
        raw = _read_text_streaming(path)
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
        _check_text_readable_streaming(path)
    except (OSError, UnicodeDecodeError) as exc:
        return refusal(
            "a2ui_data_reference_unreadable",
            detail=f"component {component_id!r} dataUri file could not be read as text: {exc}",
        )
    return None


def _validate_geojson_shape(
    path: Path, *, component_id: str, field_names: dict[str, str]
) -> dict[str, Any] | None:
    """Give the producer a typed retry hint for invalid GeoJSON and property fields."""
    try:
        parsed = json.loads(_read_text_streaming(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return refusal(
            "a2ui_data_reference_shape_invalid",
            detail=f"component {component_id!r} geojsonUri is not readable GeoJSON: {exc}",
        )
    if not isinstance(parsed, dict) or parsed.get("type") != "FeatureCollection":
        return refusal(
            "a2ui_data_reference_shape_invalid",
            detail=f"component {component_id!r} geojsonUri must contain a FeatureCollection",
        )
    features = parsed.get("features")
    if not isinstance(features, list) or not 0 < len(features) <= 20_000:
        return refusal(
            "a2ui_data_reference_shape_invalid",
            detail=f"component {component_id!r} geojsonUri needs 1 to 20000 features",
        )
    for index, feature in enumerate(features):
        if (
            not isinstance(feature, dict)
            or feature.get("type") != "Feature"
            or not isinstance(feature.get("geometry"), dict)
        ):
            return refusal(
                "a2ui_data_reference_shape_invalid",
                detail=f"component {component_id!r} geojsonUri feature {index + 1} needs geometry",
            )
    properties = [feature.get("properties") for feature in features]
    available = sorted(
        {"id", *(key for item in properties if isinstance(item, dict) for key in item)}
    )
    missing = _missing_column_labels(list(field_names.items()), available)
    if missing:
        return refusal(
            "a2ui_field_not_in_dataset",
            detail=(
                f"component {component_id!r} geojsonUri names absent feature properties: "
                f"{', '.join(missing)}; available properties: {available}"
            ),
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
        data_uri = (
            component.get("geojsonUri")
            or component.get("dataUri")
            or (component.get("meshUri") if name == _MESH_COMPONENT else None)
            or (component.get("rasterUri") if name == _RASTER_COMPONENT else None)
        )
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

        if name == _RASTER_COMPONENT:
            from clio_agent.gact.artifacts.raster_query import (  # noqa: PLC0415
                RasterQueryError,
                load_raster,
            )

            try:
                load_raster(
                    path,
                    name=record.name,
                    variable=component.get("variable"),
                    band=component.get("band", 1),
                )
            except RasterQueryError as exc:
                return refusal(
                    "a2ui_raster_invalid",
                    detail=f"component {component_id!r} rasterUri cannot be displayed: {exc}",
                )
        elif name == _MESH_COMPONENT:
            outcome = _validate_mesh_source(
                path,
                component_id=component_id,
                format_hint=component.get("format"),
                name=record.name,
            )
            if outcome is not None:
                return outcome
            material_uri = component.get("materialUri")
            if isinstance(material_uri, str):
                if component.get("format") not in (None, "obj"):
                    return refusal(
                        "a2ui_mesh_format_invalid",
                        detail=f"component {component_id!r} materialUri is only used with OBJ meshes",
                    )
                material_id = _artifact_id_from_data_uri(material_uri)
                if material_id is None:
                    continue  # The catalog reports malformed materialUri.
                material = _resolve_artifact_path(app, material_id)
                if isinstance(material, dict):
                    return material
                _, material_path = material
                try:
                    with material_path.open("rb") as stream:
                        material_text = stream.read(65536).decode("utf-8")
                except (OSError, UnicodeError) as exc:
                    return refusal(
                        "a2ui_data_reference_unreadable",
                        detail=f"component {component_id!r} materialUri could not be read as MTL: {exc}",
                    )
                if "newmtl " not in material_text:
                    return refusal(
                        "a2ui_mesh_format_invalid",
                        detail=f"component {component_id!r} materialUri has no MTL newmtl declaration",
                    )
        elif name == "clio.map.v1" and isinstance(component.get("geojsonUri"), str):
            field_names = {
                prop: component[prop]
                for prop in (
                    "labelField",
                    "detailField",
                    "categoryField",
                    "valueField",
                    "selectionField",
                )
                if isinstance(component.get(prop), str) and component[prop] != "__row"
            }
            outcome = _validate_geojson_shape(
                path, component_id=component_id, field_names=field_names
            )
            if outcome is not None:
                return outcome
        elif name in _TABULAR_COMPONENTS:
            field_props = _TABULAR_FIELD_PROPERTIES.get(name, ())
            wanted_fields = {
                prop: component[prop]
                for prop in field_props
                if isinstance(component.get(prop), str) and component[prop]
            }
            data_query = component.get("dataQuery")
            if wanted_fields or isinstance(data_query, dict):
                outcome = _validate_tabular_fields(
                    record,
                    path,
                    component_id=component_id,
                    wanted_fields=wanted_fields,
                    data_query=data_query,
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
