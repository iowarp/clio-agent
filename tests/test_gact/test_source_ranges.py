"""Byte-range handles: minted for one linked file, served on loopback, counted."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

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
