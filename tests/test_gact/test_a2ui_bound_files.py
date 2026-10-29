"""A viewer's file may follow a selection or the data model, exported like a literal (#1823)."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_update_a2ui_data_model_tool,
)
from tests.test_gact.test_a2ui_producer import _advertise_workspace_catalog, _session

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")


def _stl(path: Path) -> None:
    path.write_bytes(b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12fH", *([0.0] * 12), 0))


def _components(case_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": "root", "component": "Column", "children": ["cases", "viewer", "model"]},
        {
            "id": "cases",
            "component": "clio.data-table.v1",
            "columns": ["case", "mesh"],
            "rows": case_rows,
            "selection": {"path": "/selection/case"},
            "selectionField": "mesh",
        },
        {
            "id": "viewer",
            "component": "clio.mesh-viewport.v1",
            "meshUri": {"path": "/selection/case/values/0"},
            "format": "stl",
        },
        {
            "id": "model",
            "component": "clio.mesh-viewport.v1",
            "meshUri": {"path": "/current/mesh"},
            "format": "stl",
        },
    ]


def test_selection_rows_and_bound_model_values_are_exported(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    for name in ("c01.stl", "c02.stl", "base.stl"):
        _stl(tmp_path / name)
    created = build_create_a2ui_surface_tool()(
        surface_id="cases",
        components=_components(
            [{"case": "c01", "mesh": "c01.stl"}, {"case": "c02", "mesh": "c02.stl"}]
        ),
        data_model={
            "current": {"mesh": "base.stl"},
            "selection": {"case": {"field": "mesh", "values": ["c01.stl"]}},
        },
    )
    assert created["rendered"] is True, created
    exported = {row["path"] for row in created["exported_artifacts"]}
    assert {Path(p).name for p in exported} == {"c01.stl", "c02.stl", "base.stl"}
    surface = app.state.a2ui_store.get(sid, "cases")
    components = surface.messages[1]["updateComponents"]["components"]
    rows = next(c for c in components if c["id"] == "cases")["rows"]
    assert all(row["mesh"].startswith("artifact://") for row in rows)
    model = next(m for m in surface.messages if "updateDataModel" in m)
    assert model["updateDataModel"]["value"]["current"]["mesh"].startswith("artifact://")
    # A seeded selection is exported like the rows, so it still matches its row.
    seeded = model["updateDataModel"]["value"]["selection"]["case"]["values"][0]
    assert seeded == rows[0]["mesh"]

    update = build_update_a2ui_data_model_tool()
    moved = update(surface_id="cases", path="/current/mesh", value="c02.stl")
    assert moved["rendered"] is True
    assert moved["exported_artifacts"][0]["uri"].startswith("artifact://")
    refused = update(surface_id="cases", path="/current", value={"mesh": "https://x.org/m.stl"})
    assert refused["reason"] == "a2ui_bound_file_invalid"
    assert refused["hint"]
    missing = update(surface_id="cases", path="/current/mesh", value="nope.stl")
    assert missing["reason"] == "a2ui_url_unresolved"


def test_a_literal_mesh_path_is_exported_too(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    _stl(tmp_path / "part.stl")
    created = build_create_a2ui_surface_tool()(
        surface_id="one",
        components=[
            {"id": "root", "component": "clio.mesh-viewport.v1", "meshUri": "part.stl"},
        ],
    )
    assert created["rendered"] is True, created
    assert created["exported_artifacts"][0]["property"] == "meshUri"
