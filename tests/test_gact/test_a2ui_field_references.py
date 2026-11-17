"""Producer-boundary reference checks for the field components.

``clio.layered-map.v1`` layers, ``clio.section.v1`` and ``clio.profile-stack.v1``
name registered artifacts and columns that only the producer can see. A wrong
variable or column must come back as a typed refusal when the surface is
created, not as a blank view.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import build_create_a2ui_surface_tool
from clio_agent.gact.app import build_app

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")

PROFILES = "cast,depth,temp\nA,0,20\nA,100,10\nB,0,22\nB,100,12\n"
TRACKS = "event,time,lat,lon\nE1,2022-07-01,10,0\nE1,2022-07-02,11,1\n"


@pytest.fixture(autouse=True)
def _prewarm_pyarrow() -> None:
    """Import pyarrow at test time, as test_a2ui_data_reference explains."""

    import pyarrow  # noqa: F401


@pytest.fixture
def pin(tmp_path: Path, monkeypatch: Any) -> Any:
    """A producer session; the returned callable writes nothing, it pins a file."""

    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    client = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    root = tmp_path / "ws"
    root.mkdir()
    wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    app = client.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    caps = A2UIClientCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": [workspace_catalog_id()]}}
    )
    remember_client_capabilities(app, sid, caps)

    def _pin(name: str, write: Any) -> str:
        write(root / name)
        pinned = client.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": name})
        assert pinned.status_code == 200, pinned.text
        return f"artifact://{pinned.json()['pinned']['artifact_id']}"

    return _pin


def _text(content: str) -> Any:
    return lambda path: path.write_text(content, encoding="utf-8")


def _grid(path: Path) -> None:
    lat = np.arange(-60.0, 61, 30)
    lon = np.arange(-180.0, 180, 60)
    xr.Dataset(
        {"sst": (("latitude", "longitude"), np.add.outer(lat, lon * 0))},
        coords={"latitude": lat, "longitude": lon},
    ).to_netcdf(path)


def _section(path: Path) -> None:
    distance = np.linspace(0, 500, 5)
    pressure = np.arange(0, 100, 10.0)
    xr.Dataset(
        {"CT": (("distance", "pressure"), np.add.outer(distance, -pressure))},
        coords={
            "distance": distance,
            "pressure": ("pressure", pressure, {"positive": "down"}),
            "latitude": ("distance", np.linspace(30, 35, 5)),
            "longitude": ("distance", np.full(5, -40.0)),
        },
    ).to_netcdf(path)


def _create(components: list[dict[str, Any]]) -> dict[str, Any]:
    return build_create_a2ui_surface_tool()(surface_id="surface", components=components)


def _profile_stack(uri: str, **fields: str) -> list[dict[str, Any]]:
    spec = {"profileField": "cast", "verticalField": "depth", "valueField": "temp", **fields}
    return [{"id": "root", "component": "clio.profile-stack.v1", "dataUri": uri, **spec}]


def test_profile_stack_with_real_columns_renders(pin: Any) -> None:
    uri = pin("casts.csv", _text(PROFILES))
    result = _create(_profile_stack(uri))
    assert result.get("ok") is not False, result
    assert result["rendered"] is True


def test_profile_stack_wrong_column_is_refused_with_the_columns(pin: Any) -> None:
    uri = pin("casts.csv", _text(PROFILES))
    result = _create(_profile_stack(uri, valueField="temperature"))
    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "temperature" in result["detail"] and "temp" in result["detail"]


def _map(*layers: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": "root", "component": "clio.layered-map.v1", "layers": list(layers)}]


def test_layered_map_with_real_grid_and_track_columns_renders(pin: Any) -> None:
    grid = pin("sst.nc", _grid)
    tracks = pin("events.csv", _text(TRACKS))
    result = _create(
        _map(
            {"id": "g", "kind": "grid", "label": "SST", "gridUri": grid, "variable": "sst"},
            {
                "id": "t",
                "kind": "tracks",
                "label": "Events",
                "dataUri": tracks,
                "latitudeField": "lat",
                "longitudeField": "lon",
                "idField": "event",
                "trackField": "event",
                "timeField": "time",
            },
        )
    )
    assert result.get("ok") is not False, result


def test_layered_map_grid_variable_missing_is_refused(pin: Any) -> None:
    grid = pin("sst.nc", _grid)
    result = _create(
        _map({"id": "g", "kind": "grid", "label": "SST", "gridUri": grid, "variable": "chl"})
    )
    assert result["ok"] is False
    assert result["reason"] == "a2ui_grid_invalid"
    assert "layer 'g'" in result["detail"]


def test_layered_map_track_column_missing_names_the_layer(pin: Any) -> None:
    tracks = pin("events.csv", _text(TRACKS))
    result = _create(
        _map(
            {
                "id": "t",
                "kind": "tracks",
                "label": "Events",
                "dataUri": tracks,
                "latitudeField": "latitude",
                "longitudeField": "lon",
                "idField": "event",
                "trackField": "event",
                "timeField": "time",
            }
        )
    )
    assert result["ok"] is False
    assert result["reason"] == "a2ui_field_not_in_dataset"
    assert "layer 't'" in result["detail"]


def test_layered_map_shapes_must_be_geojson(pin: Any) -> None:
    shapes = pin("zones.geojson", _text("not json"))
    result = _create(_map({"id": "z", "kind": "shapes", "label": "Zones", "geojsonUri": shapes}))
    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_shape_invalid"


def test_section_variable_is_checked(pin: Any) -> None:
    uri = pin("section.nc", _section)
    good = _create([{"id": "root", "component": "clio.section.v1", "sectionUri": uri}])
    assert good.get("ok") is not False, good
    bad = _create(
        [{"id": "root", "component": "clio.section.v1", "sectionUri": uri, "variable": "SA"}]
    )
    assert bad["ok"] is False
    assert bad["reason"] == "a2ui_section_invalid"
    assert json.dumps(bad)  # a plain, serializable refusal
