"""A view's data source is live or pinned (taskable run_code plan, Part 3).

``artifact://artifact_<id>`` is one fixed version. A named artifact at its latest
version, ``artifact://<workspace>/<name>@latest``, is a live source: the producer
checks it against the version that is latest now, and the tool result says which
binding each chart, table or map has. A workspace path written with ``@latest``
exports the file and binds the view to it live.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_gact.test_a2ui_data_reference import (
    _create,
    _producer_session,
)

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")


@pytest.fixture(autouse=True)
def _prewarm_pyarrow() -> None:
    import pyarrow  # noqa: F401


def _workspace_id(app: Any, sid: str) -> str:
    from clio_agent.gact.artifacts.minting import _session_workspace_id

    return str(_session_workspace_id(app, sid))


def test_a_path_at_latest_exports_the_file_and_binds_the_view_live(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """SABOTAGE: drop ``_live_path`` from the export walk and the path names no file
    (``a2ui_url_unresolved``); drop the ``live`` branch in ``mint`` and the view is
    bound to the fixed version (``binding: pinned``)."""

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    (root / "status.csv").write_text("t,nozzle_c\n0,24.0\n", encoding="utf-8")

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.chart.v1",
                "preset": "scatter",
                "xField": "t",
                "yField": "nozzle_c",
                "entityField": "t",
                "dataUri": "status.csv@latest",
            }
        ]
    )

    assert result.get("ok") is not False, result
    wid = _workspace_id(app, sid)
    [exported] = result["exported_artifacts"]
    assert exported["binding"] == "live"
    assert exported["uri"] == f"artifact://{wid}/status.csv@latest"
    [binding] = result["data_bindings"]
    assert binding["binding"] == "live"
    assert binding["artifact_id"] == exported["artifact_id"]
    assert binding["version"] == 1


def test_a_live_source_is_checked_against_the_latest_version(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """SABOTAGE: resolve the named reference at its first version instead of its latest
    and the column added in v2 is refused as absent."""

    app, sid, root = _producer_session(tmp_path, monkeypatch)
    status = root / "status.csv"
    status.write_text("t,nozzle_c\n0,24.0\n", encoding="utf-8")
    first = _create([{"id": "root", "component": "clio.data-table.v1", "dataUri": "status.csv"}])
    assert first["exported_artifacts"][0]["binding"] == "pinned"
    status.write_text("t,nozzle_c,bed_c\n0,24.0,60.0\n", encoding="utf-8")
    second = _create([{"id": "root", "component": "clio.data-table.v1", "dataUri": "status.csv"}])
    assert second["exported_artifacts"][0]["version"] == 2, second

    uri = f"artifact://{_workspace_id(app, sid)}/status.csv@latest"
    result = _create(
        [
            {
                "id": "root",
                "component": "clio.chart.v1",
                "preset": "scatter",
                "xField": "t",
                "yField": "bed_c",
                "entityField": "t",
                "dataUri": uri,
            }
        ]
    )

    assert result.get("ok") is not False, result
    [binding] = result["data_bindings"]
    assert binding == {
        "component": "root",
        "dataUri": uri,
        "binding": "live",
        "artifact_id": second["exported_artifacts"][0]["artifact_id"],
        "version": 2,
    }


def test_a_live_source_naming_no_artifact_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid, _root = _producer_session(tmp_path, monkeypatch)

    result = _create(
        [
            {
                "id": "root",
                "component": "clio.data-table.v1",
                "dataUri": f"artifact://{_workspace_id(app, sid)}/missing.csv@latest",
            }
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_data_reference_not_found"
    assert "missing.csv" in result["detail"]


def test_an_artifact_id_is_reported_pinned(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, root = _producer_session(tmp_path, monkeypatch)
    (root / "rows.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = _create([{"id": "root", "component": "clio.data-table.v1", "dataUri": "rows.csv"}])

    [binding] = result["data_bindings"]
    assert binding["binding"] == "pinned"
    assert binding["dataUri"].startswith("artifact://artifact_")
    assert "version" not in binding
