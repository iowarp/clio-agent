"""Byte-range handles: minted for one linked file, served on loopback, counted."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import h5py
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.source_ranges import register_source_range_routes
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.range_handles import (
    RANGE_UNSUPPORTED,
    RangeHandle,
    handle_url,
    mint,
    registry,
)
from clio_agent.gact.storage.service import StorageService

LOOPBACK = ("127.0.0.1", 40000)


def _app_with(
    handle_bytes: bytes, *, session: str = "sess_a", ttl: float = 60
) -> tuple[FastAPI, RangeHandle]:
    app = FastAPI()
    register_source_range_routes(app)
    handle = registry(app).add(
        RangeHandle(
            id="rng_test",
            session_id=session,
            workspace_id="w",
            source_id="s",
            manifest_id="m",
            path="a.bin",
            revision="r1",
            size=len(handle_bytes),
            provider="local",
            expires_at=time.time() + ttl,
            read=lambda start, length: handle_bytes[start : start + length],
        )
    )
    return app, handle


def test_a_range_is_served_exactly_and_counted() -> None:
    app, handle = _app_with(bytes(range(256)) * 4)
    client = TestClient(app, client=LOOPBACK)
    url = "/v1/sessions/sess_a/source-ranges/rng_test"
    head = client.head(url)
    assert head.headers["content-length"] == "1024" and head.headers["accept-ranges"] == "bytes"
    part = client.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == bytes(range(10, 20))
    assert part.headers["content-range"] == "bytes 10-19/1024"
    tail = client.get(url, headers={"Range": "bytes=-4"})
    assert tail.content == bytes(range(252, 256))
    open_ended = client.get(url, headers={"Range": "bytes=1020-"})
    assert len(open_ended.content) == 4
    named = client.get(url + "/a.bin", headers={"Range": "bytes=0-1"})
    assert named.content == bytes(range(2))
    stats = client.get("/v1/sessions/sess_a/source-range-stats/rng_test").json()
    assert stats["bytes_served"] == 20 and stats["requests"] == 4


def test_a_sidecar_probe_is_not_answered_with_the_files_bytes() -> None:
    # GDAL looks for "<url>.aux.xml" and "<url>.msk"; those are other files, absent here.
    app, _ = _app_with(bytes(range(256)))
    client = TestClient(app, client=LOOPBACK)
    url = "/v1/sessions/sess_a/source-ranges/rng_test/a.bin"
    for probe in (".aux.xml", ".msk", ".msk.aux.xml"):
        assert client.head(url + probe).status_code == 404
        assert client.get(url + probe, headers={"Range": "bytes=0-1"}).status_code == 404
    assert client.get(url, headers={"Range": "bytes=0-1"}).status_code == 206


def test_a_large_file_without_range_streams_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    # GDAL with CPL_VSIL_CURL_USE_HEAD=NO learns the size from an unranged GET; a 416
    # there made it report every large tile as an unsupported format.
    from clio_agent.gact.routes import source_ranges

    monkeypatch.setattr(source_ranges, "WHOLE_READ_LIMIT", 100)
    monkeypatch.setattr(source_ranges, "STREAM_CHUNK", 64)
    body = bytes(range(256)) * 2
    app, _ = _app_with(body)
    client = TestClient(app, client=LOOPBACK)
    whole = client.get("/v1/sessions/sess_a/source-ranges/rng_test/a.bin")
    assert whole.status_code == 200 and whole.content == body
    assert whole.headers["content-length"] == str(len(body))
    stats = client.get("/v1/sessions/sess_a/source-range-stats/rng_test").json()
    assert stats["bytes_served"] == len(body) and stats["requests"] == 8


def test_the_handles_folder_is_not_answered_with_the_whole_file() -> None:
    """GDAL lists the folder on open (``…/rng_test/``, redirected to ``…/rng_test``).
    Streaming the file there made each open of a 3.3 GB raster pull all of it first.
    SABOTAGE: drop the ``not name`` refusal and the listing returns the file's bytes."""
    from clio_agent.gact.routes.source_ranges import NOT_A_LISTING

    body = bytes(range(256)) * 2
    app, _ = _app_with(body)
    client = TestClient(app, client=LOOPBACK)
    folder = "/v1/sessions/sess_a/source-ranges/rng_test"
    for url in (folder, folder + "/"):
        listing = client.get(url)
        assert listing.status_code == 404 and NOT_A_LISTING in listing.text
    assert client.get(folder, headers={"Range": "bytes=0-1"}).content == bytes(range(2))
    assert client.get(folder + "/a.bin").content == body
    stats = client.get("/v1/sessions/sess_a/source-range-stats/rng_test").json()
    assert stats["bytes_served"] == 2 + len(body)


def test_refusals_are_typed_by_status() -> None:
    app, _ = _app_with(b"x" * 100)
    loop = TestClient(app, client=LOOPBACK)
    url = "/v1/sessions/sess_a/source-ranges/rng_test"
    assert loop.get(url, headers={"Range": "bytes=200-300"}).status_code == 416
    assert loop.get(url, headers={"Range": "items=1-2"}).status_code == 416
    assert loop.get("/v1/sessions/sess_b/source-ranges/rng_test").status_code == 404
    assert TestClient(app, client=("10.0.0.5", 1)).get(url).status_code == 403
    expired_app, _ = _app_with(b"x", ttl=-1)
    assert TestClient(expired_app, client=LOOPBACK).get(url).status_code == 410


def test_mint_reads_a_linked_local_hdf5_file_by_range(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    data = np.arange(1000, dtype="f4").reshape(10, 100)
    with h5py.File(upstream / "run.h5", "w") as f:
        f.create_dataset("v", data=data, chunks=(1, 100))
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "auth.json")
    record = service.create(
        "w", CreateSource(provider="local", label="Data", root=str(upstream)), tmp_path / "ws"
    )
    record = link_folder(service, record)
    app = FastAPI()
    register_source_range_routes(app)
    app.state.api_port = 8787
    handle = mint(service, record, "run.h5", session_id="sess_a", handles=registry(app))
    assert handle_url(app, handle).startswith("http://127.0.0.1:8787/v1/sessions/sess_a/")
    assert handle_url(app, handle).endswith("/run.h5")
    client = TestClient(app, client=LOOPBACK)
    url = f"/v1/sessions/sess_a/source-ranges/{handle.id}"
    assert client.get(url, headers={"Range": "bytes=0-7"}).content == b"\x89HDF\r\n\x1a\n"
    assert handle.bytes_served == 8
    # nothing was copied into the read cache
    assert not (service.store.root / record.source.id / record.linked_manifest_id).exists()


def test_a_provider_without_seekable_reads_refuses_with_a_reason(tmp_path: Path) -> None:
    record = SimpleNamespace(source=SimpleNamespace(provider="google_drive"))
    with pytest.raises(ValueError, match=RANGE_UNSUPPORTED):
        mint(None, record, "x", session_id="s", handles=registry(FastAPI()))  # type: ignore[arg-type]


def test_a_vrt_reads_its_linked_tiles_through_its_own_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GDAL asks ``…/{handle}/TAHOE_be/he_1.img`` for a VRT's tile: served when that tile
    is a linked resource of the workspace, 404 when not, and never outside the VRT's folder.
    SABOTAGE: drop the sibling lookup in ``_handle`` and the linked tile is 404 (the live
    Tahoe run's "all 64 reads failed")."""
    from clio_agent.gact.storage import range_handles, range_siblings

    base = "https://lidar.example.org/raster/TAHOE"
    app, vrt = _app_with(b"<VRTDataset/>")
    vrt.origin, vrt.provider = f"{base}/TAHOE_be.vrt", "ndp"
    tile = SimpleNamespace(path="2-TAHOE - DTMhe_1.img", url=f"{base}/TAHOE_be/he_1.img")
    record = SimpleNamespace(source=SimpleNamespace(provider="ndp"), connected=True)
    app.state.connected_storage = SimpleNamespace(sources=lambda workspace: [record])
    monkeypatch.setattr(range_siblings, "_resources", lambda service, rec: [tile])
    minted: list[str] = []

    def mint(service: Any, rec: Any, path: str, *, session_id: str, handles: Any) -> Any:
        minted.append(path)
        return handles.add(
            RangeHandle(
                id="rng_tile", session_id=session_id, workspace_id="w", source_id="s2",
                manifest_id="m2", path=path, revision="r2", size=4, provider="ndp",
                expires_at=time.time() + 60, read=lambda start, length: b"TILE"[start:start + length],
            )
        )  # fmt: skip

    monkeypatch.setattr(range_handles, "mint", mint)
    client = TestClient(app, client=LOOPBACK)
    folder = "/v1/sessions/sess_a/source-ranges/rng_test"
    got = client.get(folder + "/TAHOE_be/he_1.img", headers={"Range": "bytes=0-1"})
    assert got.status_code == 206 and got.content == b"TI"
    client.get(folder + "/TAHOE_be/he_1.img", headers={"Range": "bytes=2-3"})
    assert minted == ["2-TAHOE - DTMhe_1.img"]  # one handle per tile, reused
    assert client.get(folder + "/TAHOE_be/he_2.img").status_code == 404  # not linked
    assert client.get(folder + "/../other/he_1.img").status_code == 404
    assert client.head(folder + "/a.bin.aux.xml").status_code == 404


def test_vrt_tiles_lists_relative_sources_once() -> None:
    from clio_agent.gact.storage.range_siblings import vrt_tiles

    text = (
        '<SourceFilename relativeToVRT="1">TAHOE_be/he_1.img</SourceFilename>'
        '<SourceFilename relativeToVRT="1">TAHOE_be/he_1.img</SourceFilename>'
        '<SourceFilename relativeToVRT="0">/abs/x.img</SourceFilename>'
        '<SourceFilename relativeToVRT="1">TAHOE_be/he%202.img</SourceFilename>'
    )
    assert vrt_tiles(text) == ["TAHOE_be/he_1.img", "TAHOE_be/he 2.img"]


def test_opening_a_vrt_says_how_many_of_its_tiles_are_linked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent learns the mosaic's tile count, how many are linked, and how to link more;
    the person sees the same count. SABOTAGE: drop the URL join in ``vrt_note`` and the
    linked count reads 0."""
    from clio_agent.gact.agents.native_presenters_inspection import source_opened
    from clio_agent.gact.storage import range_siblings

    base = "https://lidar.example.org/raster/TAHOE"
    vrt = (
        "<VRTDataset>"
        + "".join(
            f'<SourceFilename relativeToVRT="1">TAHOE_be/he_{i}.img</SourceFilename>'
            for i in range(3)
        )
        + "</VRTDataset>"
    ).encode()
    handle = RangeHandle(
        id="rng_v", session_id="s", workspace_id="w", source_id="s1", manifest_id="m",
        path="36-TAHOE - DTMTAHOE_be.vrt", revision="r", size=len(vrt), provider="ndp",
        expires_at=time.time() + 60, read=lambda start, length: vrt[start : start + length],
        origin=f"{base}/TAHOE_be.vrt",
    )  # fmt: skip
    record = SimpleNamespace(source=SimpleNamespace(provider="ndp"), connected=True)
    linked = [SimpleNamespace(path="2-x", url=f"{base}/TAHOE_be/he_1.img")]
    monkeypatch.setattr(range_siblings, "_resources", lambda service, rec: linked)
    service = SimpleNamespace(sources=lambda workspace: [record])
    note = range_siblings.vrt_note(service, handle)
    assert note is not None and note["tiles"] == 3 and note["linked"] == 1
    assert "connected_data_ndp_tiles_covering" in note["note"]
    plain = RangeHandle(**{**handle.__dict__, "path": "tile.tif"})
    assert range_siblings.vrt_note(service, plain) is None  # not a mosaic
    view = source_opened({}, {"handle": "rng_v", "path": handle.path, "size": 10, "vrt": note})
    assert "1 of 3 tile files are linked" in repr(view)
