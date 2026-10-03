"""Real grid decoders and extent resampling for the raster owner route."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.raster_query import (
    RasterQueryError,
    load_raster,
    resample_raster,
)
from clio_agent.gact.routes import artifact_raster_query as raster_route


@pytest.mark.parametrize("extension", ["npy", "csv", "tif", "nc"])
def test_raster_formats_resample_visible_extent(tmp_path: Path, extension: str) -> None:
    values = np.arange(16, dtype="float32").reshape(4, 4)
    path = tmp_path / f"grid.{extension}"
    if extension == "npy":
        np.save(path, values)
    elif extension == "csv":
        np.savetxt(path, values, delimiter=",")
    elif extension == "tif":
        import rasterio
        from rasterio.transform import from_origin

        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=4,
            height=4,
            count=1,
            dtype="float32",
            transform=from_origin(0, 4, 1, 1),
        ) as target:
            target.write(values, 1)
    else:
        import xarray as xr

        xr.Dataset({"temperature": (("northing", "easting"), values)}).to_netcdf(path)
    grid = load_raster(
        path, name=path.name, variable="temperature" if extension == "nc" else None, band=1
    )
    response = resample_raster(grid, width=2, height=2, extent=None)
    assert response["values"] == [5.0, 7.0, 13.0, 15.0]
    assert response["sourceShape"] == [4, 4]
    assert response["extent"] == list(grid.bounds)


def test_raster_subset_and_nodata_are_truthful(tmp_path: Path) -> None:
    path = tmp_path / "grid.npy"
    np.save(path, np.array([[1.0, np.nan], [3.0, 4.0]]))
    grid = load_raster(path, name=path.name, variable=None, band=1)
    result = resample_raster(grid, width=2, height=2, extent=None)
    assert result["values"] == [1.0, None, 3.0, 4.0]
    assert result["min"] == 1.0 and result["max"] == 4.0
    outside = resample_raster(grid, width=1, height=1, extent=(5, 0, 6, 1))
    assert outside["values"] == [None]


def test_raster_rejects_non_grid_shape(tmp_path: Path) -> None:
    path = tmp_path / "stack.npy"
    np.save(path, np.ones((2, 3, 4)))
    with pytest.raises(RasterQueryError, match="exactly two dimensions"):
        load_raster(path, name=path.name, variable=None, band=1)


def test_zipped_zarr_variable_is_sampled(tmp_path: Path) -> None:
    import xarray as xr

    path = tmp_path / "grid.zarr.zip"
    folder = tmp_path / "grid.zarr"
    xr.Dataset({"temperature": (("y", "x"), np.arange(16).reshape(4, 4))}).to_zarr(
        folder, mode="w", consolidated=False
    )
    with ZipFile(path, "w") as archive:
        for file in folder.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(folder))
    grid = load_raster(path, name=path.name, variable="temperature", band=1)
    assert resample_raster(grid, width=2, height=2, extent=None)["values"] == [
        5.0,
        7.0,
        13.0,
        15.0,
    ]


def test_registered_raster_route_returns_resampled_values(tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
    workspace = client.post("/v1/workspaces", json={"name": "raster", "root_path": str(tmp_path)})
    assert workspace.status_code == 201, workspace.text
    session = client.post("/v1/sessions", json={"workspace_id": workspace.json()["id"]})
    assert session.status_code == 200, session.text
    np.save(tmp_path / "field.npy", np.arange(16, dtype="float32").reshape(4, 4))
    pinned = client.post(
        f"/v1/sessions/{session.json()['id']}/artifacts/pin", json={"path": "field.npy"}
    )
    assert pinned.status_code == 200, pinned.text
    artifact_id = pinned.json()["pinned"]["artifact_id"]
    response = client.post(
        f"/v1/artifacts/{artifact_id}/raster-query", json={"width": 2, "height": 2}
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"] == [5.0, 7.0, 13.0, 15.0]
    bad = client.post(f"/v1/artifacts/{artifact_id}/raster-query", json={"width": 0, "height": 2})
    assert bad.status_code == 422


def test_timed_out_raster_worker_keeps_its_permit_until_it_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = Event()
    release = Event()

    def slow_query(path: Path, name: str, body: raster_route.RasterQueryRequest) -> dict[str, int]:
        started.set()
        assert release.wait(2)
        return {"width": body.width}

    monkeypatch.setattr(raster_route, "_query", slow_query)

    async def exercise() -> None:
        semaphore = asyncio.Semaphore(1)
        body = raster_route.RasterQueryRequest(width=2)
        first = asyncio.create_task(
            raster_route._run_bounded_query(semaphore, tmp_path, "grid.npy", body, timeout=0.02)
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            with pytest.raises(TimeoutError):
                await first
            assert semaphore.locked()
            second = asyncio.create_task(
                raster_route._run_bounded_query(semaphore, tmp_path, "grid.npy", body, timeout=0.5)
            )
            await asyncio.sleep(0.03)
            assert not second.done()
            release.set()
            assert await second == {"width": 2}
            await asyncio.sleep(0)
            assert not semaphore.locked()
        finally:
            release.set()

    asyncio.run(exercise())
