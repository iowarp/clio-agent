"""Data-reference checks for the field components (layered map, section, profile stack).

Like the checks in :mod:`._data_reference`, these run at the producer
boundary where the referenced files are available: every artifact a field
component names must exist and be readable, a grid or section variable must
be one the file holds, and every ``*Field`` must be a real column of the
referenced table. A wrong name comes back as a typed refusal at creation time
instead of a blank view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_producer._data_reference import (
    _artifact_id_from_data_uri,
    _resolve_artifact_path,
    _validate_geojson_shape,
    _validate_tabular_fields,
)
from clio_agent.gact.a2ui_producer._live_sources import latest_artifact_id, record_binding
from clio_agent.gact.a2ui_producer._refusal import refusal

if TYPE_CHECKING:
    from fastapi import FastAPI

_TABLE_LAYER_FIELDS = (
    "latitudeField",
    "longitudeField",
    "idField",
    "labelField",
    "timeField",
    "trackField",
    "categoryField",
    "valueField",
)
_PROFILE_FIELDS = (
    "profileField",
    "verticalField",
    "valueField",
    "overlayField",
    "labelField",
    "categoryField",
    "selectionField",
)


def _resolve(
    app: FastAPI, uri: Any, component_id: str, bindings: list[dict[str, Any]] | None
) -> Any:
    """(record, version, path) for an artifact URI, a refusal dict, or None to skip."""
    if not isinstance(uri, str) or not uri.startswith("artifact://"):
        return None
    latest = latest_artifact_id(app, uri, component_id)
    if isinstance(latest, dict):
        return latest
    artifact_id = latest[0] if latest else _artifact_id_from_data_uri(uri)
    if artifact_id is None:
        return None  # malformed: the catalog validator reports it
    record_binding(
        bindings,
        component_id,
        uri,
        "live" if latest else "pinned",
        artifact_id=artifact_id,
        **({"version": latest[1]} if latest else {}),
    )
    return _resolve_artifact_path(app, artifact_id)


def _table(
    app: FastAPI, spec: dict[str, Any], fields: tuple[str, ...], where: str, bindings: Any
) -> Any:
    resolved = _resolve(app, spec.get("dataUri"), where, bindings)
    if resolved is None or isinstance(resolved, dict):
        return resolved
    record, version, path = resolved
    wanted = {prop: spec[prop] for prop in fields if isinstance(spec.get(prop), str) and spec[prop]}
    variables = spec.get("variables")
    if isinstance(variables, list):
        wanted.update({f"variables[{i}]": v for i, v in enumerate(variables) if isinstance(v, str)})
    return _validate_tabular_fields(
        record,
        version,
        path,
        component_id=where,
        wanted_fields=wanted,
        data_query=spec.get("dataQuery"),
    )


def _grid(app: FastAPI, layer: dict[str, Any], where: str, bindings: Any) -> Any:
    from clio_agent.gact.artifacts.raster_query import RasterQueryError  # noqa: PLC0415
    from clio_agent.gact.artifacts.raster_tiles import grid_info  # noqa: PLC0415

    resolved = _resolve(app, layer.get("gridUri"), where, bindings)
    if resolved is None or isinstance(resolved, dict):
        return resolved
    record, _version, path = resolved
    try:
        grid_info(path, variable=layer.get("variable"), select=layer.get("fixed"), name=record.name)
    except RasterQueryError as exc:
        return refusal(
            "a2ui_grid_invalid", detail=f"{where} gridUri cannot be drawn on a map: {exc}"
        )
    return None


def _section(app: FastAPI, component: dict[str, Any], where: str, bindings: Any) -> Any:
    from clio_agent.gact.artifacts.raster_query import RasterQueryError  # noqa: PLC0415
    from clio_agent.gact.artifacts.raster_tiles import section_sample  # noqa: PLC0415

    resolved = _resolve(app, component.get("sectionUri"), where, bindings)
    if resolved is None or isinstance(resolved, dict):
        return resolved
    record, _version, path = resolved
    names = [component.get("variable"), *(component.get("variables") or [])]
    variables: list[str | None] = [n for n in names if isinstance(n, str)] or [None]
    for variable in variables:
        try:
            section_sample(path, variable=variable, width=2, height=2, name=record.name)
        except RasterQueryError as exc:
            return refusal(
                "a2ui_section_invalid", detail=f"{where} sectionUri cannot be shown: {exc}"
            )
    return None


def _shapes(app: FastAPI, layer: dict[str, Any], where: str, bindings: Any) -> Any:
    resolved = _resolve(app, layer.get("geojsonUri"), where, bindings)
    if resolved is None or isinstance(resolved, dict):
        return resolved
    return _validate_geojson_shape(resolved[2], component_id=where, field_names={})


def validate_field_component_references(
    app: FastAPI, components: list[dict[str, Any]], bindings: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """The first typed refusal among the field components' references, else None."""
    for component in components:
        if not isinstance(component, dict):
            continue
        name = component.get("component")
        component_id = str(component.get("id") or "")
        where = f"component {component_id!r}"
        outcome: Any = None
        if name == "clio.profile-stack.v1":
            outcome = _table(app, component, _PROFILE_FIELDS, where, bindings)
        elif name == "clio.section.v1":
            outcome = _section(app, component, where, bindings)
        elif name == "clio.layered-map.v1":
            for layer in component.get("layers") or []:
                if not isinstance(layer, dict):
                    continue
                here = f"{where} layer {layer.get('id')!r}"
                kind = layer.get("kind")
                if kind == "grid":
                    outcome = _grid(app, layer, here, bindings)
                elif kind in ("points", "tracks"):
                    outcome = _table(app, layer, _TABLE_LAYER_FIELDS, here, bindings)
                elif kind == "shapes":
                    outcome = _shapes(app, layer, here, bindings)
                if outcome is not None:
                    break
        if outcome is not None:
            return outcome
    return None


__all__ = ["validate_field_component_references"]
