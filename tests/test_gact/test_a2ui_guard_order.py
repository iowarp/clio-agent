"""Every door bounds a message before the catalog's JSON Schema, and runs the chart guard."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.a2ui import A2UIValidationError, validate_server_message
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

CATALOGS = CatalogRegistry()


def _update(component: dict[str, Any]) -> None:
    validate_server_message(
        {"version": "v0.9.1", "updateComponents": {"surfaceId": "s", "components": [component]}},
        catalogs=CATALOGS,
        catalog_entry=CATALOGS.get(workspace_catalog_id()),
    )


def _chart(**fields: Any) -> dict[str, Any]:
    return {"id": "c", "component": "clio.chart.v1", **fields}


def test_deep_geometry_nesting_is_refused_not_a_crash() -> None:
    cell: dict[str, Any] = {"type": "Point", "coordinates": [0, 0]}
    for _ in range(300):
        cell = {"type": "GeometryCollection", "geometries": [cell]}
    spec = {"mark": "geoshape", "encoding": {"shape": {"field": "g", "type": "geojson"}}}
    with pytest.raises(A2UIValidationError):
        _update(_chart(data=[{"g": cell}], spec=spec))


def test_the_chart_guard_runs_outside_the_producer_tool() -> None:
    nested = {"mark": "point", "layer": [{"mark": {"type": "point", "url": "https://x.test/"}}]}
    with pytest.raises(A2UIValidationError, match="chart guard"):
        _update(_chart(data=[{"t": 0, "v": 1}], spec=nested))
    with pytest.raises(A2UIValidationError, match="chart guard"):
        _update(_chart(data=[{"t": 0}], spec={"mark": "point", "transform": [0] * 40_000}))
    _update(
        _chart(
            data=[{"t": 0, "v": 1}],
            spec={
                "mark": "point",
                "encoding": {
                    "x": {"field": "t", "type": "quantitative"},
                    "y": {"field": "v", "type": "quantitative"},
                },
            },
        )
    )


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "//x.test/i.png"])
def test_a_theme_icon_url_is_a_url_slot(url: str) -> None:
    with pytest.raises(A2UIValidationError):
        validate_server_message(
            {
                "version": "v0.9.1",
                "createSurface": {
                    "surfaceId": "s",
                    "catalogId": workspace_catalog_id(),
                    "theme": {"iconUrl": url},
                },
            },
            catalogs=CATALOGS,
        )
