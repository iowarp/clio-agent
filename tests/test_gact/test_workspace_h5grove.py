"""The workspace h5grove route: h5grove's API over real HDF5 files, inside the workspace.

Files resolve exactly as ``GET /files/read`` resolves them, except that the
whole-file size cap gives way to a per-response bound. Inside a file, external
links, soft links and virtual datasets are followed only into the workspace.
"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from clio_agent.gact.app import build_app

BASE = "/v1/workspaces/ws_default/h5grove"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    client = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    project = tmp_path / "project"
    project.mkdir()
    client.app.state.workspaces.update("ws_default", root_path=str(project))
    return client


def _project(client: TestClient) -> Path:
    return Path(client.app.state.workspaces.get("ws_default").root_path)


def _write(path: Path, build) -> Path:  # type: ignore[no-untyped-def]
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as h5:
        build(h5)
    return path


def _run(client: TestClient) -> Path:
    """A NeXus-like file: data, attributes, a soft link and an external link inside."""

    project = _project(client)
    _write(project / "data" / "frames.h5", lambda h5: h5.create_dataset("frames", data=np.ones(4)))

    def build(h5: h5py.File) -> None:
        entry = h5.create_group("entry")
        entry.attrs["NX_class"] = "NXentry"
        entry.create_dataset("I", data=np.arange(12, dtype="f8").reshape(3, 4))
        entry["alias"] = h5py.SoftLink("/entry/I")
        entry["detector"] = h5py.ExternalLink("frames.h5", "/frames")

    return _write(project / "data" / "run.h5", build)


def _get(client: TestClient, endpoint: str, **params: object):  # type: ignore[no-untyped-def]
    return client.get(f"{BASE}/{endpoint}", params={"file": "data/run.h5", **params})


def test_capabilities_advertise_the_viewer(client: TestClient) -> None:
    v3 = client.get("/v1/capabilities", headers={"X-GACT-Version": "0.3"}).json()

    assert client.get("/v1/capabilities").json()["capabilities"]["x_clio_hdf5_viewer"] is True
    assert v3["capabilities"]["x_clio_hdf5_viewer"] is True


def test_meta_attr_and_data_serve_the_file(client: TestClient) -> None:
    _run(client)

    meta = _get(client, "meta", path="/entry")
    attrs = _get(client, "attr", path="/entry")
    data = _get(client, "data", path="/entry/I", selection="1,:")
    binary = _get(client, "data", path="/entry/I", dtype="safe", format="bin")
    stats = _get(client, "stats", path="/entry/I")

    assert meta.status_code == 200, meta.text
    children = {child["name"]: child["kind"] for child in meta.json()["children"]}
    assert children == {"I": "dataset", "alias": "dataset", "detector": "dataset"}
    assert attrs.json() == {"NX_class": "NXentry"}
    assert data.json() == [4.0, 5.0, 6.0, 7.0]
    assert binary.headers["content-type"] == "application/octet-stream"
    assert len(binary.content) == 12 * 8
    assert stats.json()["max"] == 11.0


def test_links_inside_the_workspace_are_followed(client: TestClient) -> None:
    _run(client)

    soft = _get(client, "data", path="/entry/alias", selection="0,0")
    external = _get(client, "data", path="/entry/detector")
    paths = _get(client, "paths", path="/")

    assert soft.json() == 0.0
    assert external.json() == [1.0, 1.0, 1.0, 1.0]
    assert paths.json() == ["/", "/entry", "/entry/I", "/entry/alias", "/entry/detector"]


def test_an_external_link_out_of_the_workspace_is_never_followed(
    client: TestClient, tmp_path: Path
) -> None:
    secret = _write(tmp_path / "secret.h5", lambda h5: h5.create_dataset("s", data=[42]))

    def build(h5: h5py.File) -> None:
        h5["leak"] = h5py.ExternalLink(str(secret), "/s")
        h5["deep"] = h5py.ExternalLink(str(secret), "/")

    _write(_project(client) / "data" / "run.h5", build)

    listing = _get(client, "meta", path="/")
    leak_meta = _get(client, "meta", path="/leak")
    leak_data = _get(client, "data", path="/leak")
    through = _get(client, "data", path="/deep/s")

    children = {child["name"]: child["kind"] for child in listing.json()["children"]}
    assert children == {"deep": "external_link", "leak": "external_link"}
    assert leak_meta.json()["kind"] == "external_link"
    assert leak_data.status_code == 403
    assert leak_data.json()["error"]["error"] == "hdf5_link_outside_workspace"
    assert through.status_code == 403
    assert through.json()["error"]["error"] == "hdf5_link_outside_workspace"
    assert "42" not in leak_data.text + through.text


def test_a_soft_link_cannot_route_through_an_outside_external_link(
    client: TestClient, tmp_path: Path
) -> None:
    secret = _write(tmp_path / "secret.h5", lambda h5: h5.create_dataset("s", data=[42]))

    def build(h5: h5py.File) -> None:
        h5["ext"] = h5py.ExternalLink(str(secret), "/")
        h5["innocent"] = h5py.SoftLink("/ext/s")

    _write(_project(client) / "data" / "run.h5", build)

    response = _get(client, "data", path="/innocent")

    assert response.status_code == 403
    assert response.json()["error"]["error"] == "hdf5_link_outside_workspace"


def test_a_virtual_dataset_reading_outside_the_workspace_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    secret = _write(tmp_path / "secret.h5", lambda h5: h5.create_dataset("s", data=np.arange(4)))

    def build(h5: h5py.File) -> None:
        layout = h5py.VirtualLayout(shape=(4,), dtype="i8")
        layout[:] = h5py.VirtualSource(str(secret), "s", shape=(4,))
        h5.create_virtual_dataset("v", layout)

    _write(_project(client) / "data" / "run.h5", build)

    meta = _get(client, "meta", path="/v")
    data = _get(client, "data", path="/v")

    assert meta.status_code == 200
    assert data.status_code == 403
    assert data.json()["error"]["error"] == "hdf5_virtual_source_outside_workspace"


def test_a_large_file_opens_and_only_an_oversized_slice_is_refused(
    client: TestClient, monkeypatch: MonkeyPatch
) -> None:
    _write(
        _project(client) / "data" / "run.h5",
        lambda h5: h5.create_dataset("big", data=np.zeros((100, 100))),
    )
    # The file is larger than the cap, but the cap bounds each response, not the file.
    monkeypatch.setenv("CLIO_MAX_FILE_SIZE_BYTES", "4096")

    meta = _get(client, "meta", path="/big")
    row = _get(client, "data", path="/big", selection="0,:")
    whole = _get(client, "data", path="/big")

    assert meta.status_code == 200
    assert row.status_code == 200 and len(row.json()) == 100
    assert whole.status_code == 413
    assert whole.json()["error"]["error"] == "hdf5_selection_too_large"


def test_typed_errors_for_bad_requests(client: TestClient) -> None:
    _run(client)
    (_project(client) / "notes.h5").write_text("not hdf5")

    missing_path = _get(client, "meta", path="/nope")
    group_data = _get(client, "data", path="/entry")
    bad_selection = _get(client, "data", path="/entry/I", selection="1,2,3")
    not_hdf5 = client.get(f"{BASE}/meta", params={"file": "notes.h5"})

    assert missing_path.status_code == 404
    assert missing_path.json()["error"]["error"] == "hdf5_path_not_found"
    assert group_data.json()["error"]["error"] == "hdf5_not_a_dataset"
    assert bad_selection.json()["error"]["error"] == "hdf5_invalid_selection"
    assert not_hdf5.json()["error"]["error"] == "hdf5_unreadable"


def test_files_resolve_as_files_read_does(client: TestClient) -> None:
    (_project(client).parent / "outside.h5").write_bytes(b"\x89HDF\r\n\x1a\n")
    cache = _project(client) / ".clio-child-cache"
    cache.mkdir()
    (cache / "run.h5").write_bytes(b"\x89HDF\r\n\x1a\n")

    outside = client.get(f"{BASE}/meta", params={"file": "../outside.h5"})
    redacted = client.get(f"{BASE}/meta", params={"file": ".clio-child-cache/run.h5"})
    missing = client.get(f"{BASE}/meta", params={"file": "absent.h5"})
    unknown = client.get("/v1/workspaces/ws_nope/h5grove/meta", params={"file": "x.h5"})

    assert outside.json()["error"]["error"] == "path_outside_workspace"
    assert redacted.json()["error"]["details"]["reason"] == "sandbox_child_cache"
    assert missing.status_code == 404 and missing.json()["error"]["error"] == "not_found"
    assert unknown.status_code == 404 and unknown.json()["error"]["error"] == "not_found"
