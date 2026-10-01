"""The stable ``__row`` key table-query adds to every non-aggregated result.

G0 (built-in surface affordances): two data views over the SAME ``dataUri``
must be able to link a selection without the agent binding anything, through
a row key the server assigns -- not a business column either view happens to
project. These tests pin that contract directly against the engine and the
HTTP route.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts import table_query as engine

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

    Duplicated (not imported) from ``test_artifact_table_query.py``: the
    `tests/test_gact/conftest.py`'s ``_default_test_arc`` autouse fixture
    wires a default in-memory ARC onto ``build_app`` by monkeypatching that
    NAME on ``request.module`` -- it must be imported directly into THIS
    module (not borrowed from another module's closure) for that patch to
    reach the ``build_app(...)`` call below.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))
        workspace_id = self.client.post(
            "/v1/workspaces",
            json={"name": "chart data", "root_path": str(tmp_path)},
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


def _cancellation() -> engine.QueryCancellation:
    import time

    return engine.QueryCancellation(deadline=time.monotonic() + 30, timeout_s=30)


def test_row_key_present_and_zero_based_for_a_plain_query(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(env.query(artifact_id, {"columns": ["sensor", "t"]}))

    assert "rowKey" in body
    assert body["rowKey"]["column"] == "__row"
    assert body["rowKey"]["values"] == list(range(7))


def test_row_key_survives_filter_and_keeps_original_identity(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {"columns": ["sensor"], "filter": [{"column": "sensor", "op": "eq", "value": "b"}]},
        )
    )

    # Rows for sensor "b" are source rows 3, 4, 5 (0-indexed) in _SENSORS_CSV.
    assert body["rowKey"]["values"] == [3, 4, 5]


def test_row_key_survives_sort_reordering(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "t"],
                "filter": [{"column": "sensor", "op": "eq", "value": "a"}],
                "sort": [{"column": "t", "desc": True}],
            },
        )
    )

    # Source rows 0,1,2 (t=0,1,2) sorted descending by t -> rows 2,1,0.
    assert body["rowKey"]["values"] == [2, 1, 0]


def test_row_key_absent_when_aggregated(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "value_mean"],
                "aggregate": {
                    "groupBy": ["sensor"],
                    "metrics": [{"column": "value", "fn": "mean"}],
                },
            },
        )
    )

    assert "rowKey" not in body


def test_row_key_column_name_avoids_collision_with_a_real_column(env: _Env) -> None:
    csv_text = "__row,value\n5,1.0\n6,2.0\n"
    artifact_id = env.pin_csv("collide.csv", csv_text)

    body = _ok(env.query(artifact_id, {"columns": ["__row", "value"]}))

    # The real "__row" column is returned untouched under its own name in
    # `columns`; the synthetic key is reported under a different name.
    assert body["columns"]["__row"] == [5, 6]
    assert body["rowKey"]["column"] != "__row"
    assert body["rowKey"]["values"] == [0, 1]


def test_two_queries_over_the_same_artifact_agree_on_row_identity(env: _Env) -> None:
    """Different column projections of the SAME artifact must key the same
    underlying row identically -- the basis for auto-linking two views."""

    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    by_sensor = _ok(env.query(artifact_id, {"columns": ["sensor"]}))
    by_value = _ok(env.query(artifact_id, {"columns": ["value"]}))

    assert by_sensor["rowKey"]["values"] == by_value["rowKey"]["values"]
