"""A dashboard export streams its files by reference: memory stays at a chunk (#1823)."""

from __future__ import annotations

import base64
import gzip
import json
import re
import struct
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.artifacts.designation_tool import build_create_artifact_tool
from clio_agent.gact.dashboard_export import CHUNK_BYTES, DashboardExportRefused
from clio_agent.gact.dashboard_reports import (
    export_dashboard_report,
    publish_dashboard_report,
    read_dashboard_report,
)
from clio_agent.gact.types import AgentDef
from tests.test_gact.test_dashboard_reports import dashboard  # noqa: F401

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")

_BLOCK = re.compile(
    rb'<script type="application/octet-stream" id="(clio-bytes-\d+)">([A-Za-z0-9+/=]*)</script>'
)


def _stl(path: Path, triangles: int, seed: int) -> None:
    """A valid binary STL of ``triangles`` facets (50 bytes each)."""

    facet = struct.pack("<12fH", *([float(seed)] * 12), 0)
    with open(path, "wb") as out:
        out.write(b"\0" * 80 + struct.pack("<I", triangles))
        for _ in range(triangles // 1000):
            out.write(facet * 1000)


def _published(app: Any, sid: str, path: Path, sizes: list[int]) -> tuple[dict[str, Any], dict]:
    create = build_create_artifact_tool(AgentDef(id="mesh-source", title="CLIO")).func
    meshes: dict[str, Path] = {}
    for index, triangles in enumerate(sizes):
        mesh = path.parent / f"case_{index}.stl"
        _stl(mesh, triangles, index)
        registered = create(path=str(mesh), pdf_preview=False)
        meshes[registered["artifacts"][0]["artifact_id"]] = mesh
    source = json.loads(path.read_text())
    source["components"] = [
        {"id": "root", "component": "Column", "children": [f"m{i}" for i in range(len(sizes))]},
        *[
            {"id": f"m{i}", "component": "clio.mesh-viewport.v1", "meshUri": f"artifact://{aid}"}
            for i, aid in enumerate(meshes)
        ],
    ]
    path.write_text(json.dumps(source))
    saved = publish_dashboard_report(app, sid, definition_path=str(path))
    report = read_dashboard_report(app, sid, saved["artifact_id"])
    surface = {
        key: value
        for key, value in report["surface"].items()
        if key not in {"created_at", "updated_at", "evicted_messages", "actions"}
    }
    renderer = {
        "javascript": "window.dashboardStarted=true;",
        "stylesheet": "",
        "snapshot": {
            "sessions": {sid: [surface]},
            "responses": {f"GET /v1/artifacts/{aid}/bytes": {"artifact": aid} for aid in meshes},
            "tables": {},
            "failures": [],
        },
    }
    return {"saved": saved, "renderer": renderer}, meshes


@pytest.mark.timeout(120)  # writes ~45 MB of meshes and reads them back twice
def test_export_copies_files_by_reference_in_bounded_memory(
    dashboard: tuple[Any, str, Path],  # noqa: F811
) -> None:
    app, sid, path = dashboard
    # Seven CAS-held meshes (5 MB) and one workspace-referenced mesh over 16 MiB.
    made, meshes = _published(app, sid, path, [100_000] * 7 + [400_000])
    total = sum(mesh.stat().st_size for mesh in meshes.values())
    tracemalloc.start()
    try:
        page, _ = export_dashboard_report(app, sid, made["saved"]["artifact_id"], made["renderer"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Peak memory follows the chunk, not the files: well under one file, let alone all.
    assert peak < 6 * CHUNK_BYTES < total
    html = page.read_bytes()
    page.unlink()
    blocks = dict(_BLOCK.findall(html))
    assert len(blocks) == len(meshes)
    data = re.search(rb'window\.CLIO_DASHBOARD_DATA="([^"]+)"', html)
    assert data is not None
    snapshot = json.loads(gzip.decompress(base64.b64decode(data.group(1))))["snapshot"]
    for aid, mesh in meshes.items():
        element = snapshot["responses"][f"GET /v1/artifacts/{aid}/bytes"]["embedded"]
        assert base64.b64decode(blocks[element.encode()]) == mesh.read_bytes()


def test_an_export_over_its_budget_is_refused_with_its_size(
    dashboard: tuple[Any, str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, sid, path = dashboard
    made, _ = _published(app, sid, path, [20_000, 20_000])
    monkeypatch.setenv("CLIO_DASHBOARD_EXPORT_MAX_BYTES", "1000000")
    with pytest.raises(DashboardExportRefused) as refused:
        export_dashboard_report(app, sid, made["saved"]["artifact_id"], made["renderer"])
    assert refused.value.code == "dashboard_export_too_large"
    assert refused.value.details["total_bytes"] > 1_000_000
    with TestClient(app) as client:
        response = client.post(
            f"/v1/sessions/{sid}/dashboards/{made['saved']['artifact_id']}/export",
            json=made["renderer"],
        )
    assert response.status_code == 413, response.text
    error = response.json()["error"]
    assert error["error"] == "dashboard_export_too_large"
    assert error["details"]["budget_bytes"] == 1_000_000


def test_an_export_names_only_files_of_its_own_workspace(
    dashboard: tuple[Any, str, Path],  # noqa: F811
) -> None:
    app, sid, path = dashboard
    made, _ = _published(app, sid, path, [1_000])
    made["renderer"]["snapshot"]["responses"]["GET /v1/artifacts/artifact_nope/bytes"] = {
        "artifact": "artifact_nope"
    }
    with pytest.raises(DashboardExportRefused) as refused:
        export_dashboard_report(app, sid, made["saved"]["artifact_id"], made["renderer"])
    assert refused.value.code == "dashboard_export_file_unavailable"
