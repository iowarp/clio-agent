"""``POST /v1/artifacts/{id}/table-export``: CSV/JSON/Parquet download, never sampled.

G0 (built-in surface affordances): "download, done by the client or server,
never agent code" -- for a ``dataUri`` table/chart/map this is the server
route, reusing table-query's own filter/sort/columns engine so the download
always matches what the viewer is looking at. These tests round-trip CSV
against the server-filtered JSON result, cover JSON/Parquet formats, the
``full`` scope (ignoring the current filter), and the no-silent-sampling
refusal.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.app import build_app

_SENSORS_CSV = (
    "sensor,t,value,flag,note\n"
    "a,0,1.0,1,x\n"
    "a,1,2.0,0,\n"
    "a,2,3.0,1,y\n"
    "b,0,10.0,0,x\n"
    "b,1,NaN,1,\n"
    "b,2,30.0,0,z\n"
    "c,0,100.0,1,x\n"
)


@pytest.fixture(autouse=True)
def _fresh_store():
    """Reset the process-wide config store around each test."""

    conf.reload()
    yield
    conf.reload()


class _Env:
    """A test app with one workspace + session able to pin files as artifacts.

    Duplicated (not imported) from ``test_artifact_table_query.py``: see that
    file's own ``_Env`` docstring note in ``test_table_query_row_key.py`` for
    why -- the conftest's default-ARC fixture patches ``build_app`` by name
    on THIS module, so it must be imported here directly.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
        workspace_id = self.client.post(
            "/v1/workspaces",
            json={"name": "export data", "root_path": str(tmp_path)},
        ).json()["id"]
        self.session_id = self.client.post(
            "/v1/sessions",
            json={"workspace_id": workspace_id},
        ).json()["id"]

    def pin(self, name: str, write: Callable[[Path], None]) -> str:
        source = self.root / name
        write(source)
        response = self.client.post(
            f"/v1/sessions/{self.session_id}/artifacts/pin",
            json={"path": name},
        )
        assert response.status_code == 200, response.text
        return str(response.json()["pinned"]["artifact_id"])

    def pin_csv(self, name: str, text: str) -> str:
        return self.pin(name, lambda path: path.write_text(text, encoding="utf-8"))

    def query(self, artifact_id: str, body: dict[str, Any]) -> Any:
        return self.client.post(f"/v1/artifacts/{artifact_id}/table-query", json=body)


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


def _export(env: _Env, artifact_id: str, body: dict[str, Any]) -> Any:
    return env.client.post(f"/v1/artifacts/{artifact_id}/table-export", json=body)


def _rows_from_csv(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def test_csv_export_current_scope_round_trips_the_server_filtered_result(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    query_body = {
        "columns": ["sensor", "t", "value"],
        "filter": [{"column": "sensor", "op": "eq", "value": "a"}],
    }

    queried = _ok(env.query(artifact_id, query_body))
    response = _export(env, artifact_id, {**query_body, "scope": "current", "format": "csv"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    rows = _rows_from_csv(response.content)
    assert [row["sensor"] for row in rows] == queried["columns"]["sensor"]
    assert [row["t"] for row in rows] == [str(v) for v in queried["columns"]["t"]]
    assert response.headers["x-clio-export-returned-rows"] == str(len(rows))


def test_json_export_current_scope_is_row_oriented(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env, artifact_id, {"columns": ["sensor", "value"], "scope": "current", "format": "json"}
    )

    assert response.status_code == 200, response.text
    payload = json.loads(response.content)
    assert payload[0] == {"sensor": "a", "value": 1.0}
    assert len(payload) == 7


def test_parquet_export_current_scope_is_readable(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env, artifact_id, {"columns": ["sensor", "value"], "scope": "current", "format": "parquet"}
    )

    assert response.status_code == 200, response.text
    table = pq.read_table(pa.BufferReader(response.content))
    assert table.column("sensor").to_pylist()[:3] == ["a", "a", "a"]


def test_full_scope_ignores_the_current_filter(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env,
        artifact_id,
        {
            "columns": ["sensor"],
            "filter": [{"column": "sensor", "op": "eq", "value": "a"}],
            "scope": "full",
            "format": "json",
        },
    )

    payload = json.loads(response.content)
    assert len(payload) == 7  # every row, the filter is NOT applied in "full" scope
    assert {row["sensor"] for row in payload} == {"a", "b", "c"}


def test_export_too_large_refuses_rather_than_silently_sampling(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.routes import artifact_table_export as route

    monkeypatch.setattr(route, "table_export_max_rows", lambda: 2)
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env, artifact_id, {"columns": ["sensor"], "scope": "current", "format": "csv"}
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["error"] == "export_too_large"


def test_export_unknown_artifact_is_404(env: _Env) -> None:
    response = _export(env, "artifact_missing", {"scope": "current", "format": "csv"})
    assert response.status_code == 404, response.text


def test_export_unsupported_format_media_type_for_non_tabular_artifact(env: _Env) -> None:
    artifact_id = env.pin("notes.txt", lambda path: path.write_text("hello", encoding="utf-8"))

    response = _export(env, artifact_id, {"scope": "current", "format": "csv"})

    assert response.status_code == 415, response.text
