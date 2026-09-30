"""``POST /v1/artifacts/{id}/table-query``: bounded query/downsample for charts."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts import table_query as engine
from clio_agent.gact.routes import artifact_table_query as route
from tests._config_layer import set_config


@pytest.fixture(autouse=True)
def _fresh_store():
    """Reset the process-wide config store around each test."""

    conf.reload()
    yield
    conf.reload()


class _Env:
    """A test app with one workspace + session able to pin files as artifacts."""

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

    def pin_parquet(self, name: str, table: pa.Table) -> str:
        return self.pin(name, lambda path: pq.write_table(table, path))

    def query(self, artifact_id: str, body: dict[str, Any]) -> Any:
        return self.client.post(f"/v1/artifacts/{artifact_id}/table-query", json=body)


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


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


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


def _error(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()["error"]


# --------------------------------------------------------------------------- #
# Projection / formats
# --------------------------------------------------------------------------- #


def test_csv_projection_returns_only_requested_columns(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(env.query(artifact_id, {"columns": ["sensor", "value"]}))

    assert [field["name"] for field in body["schema"]] == ["sensor", "value"]
    assert body["schema"][1]["type"] == "double"
    assert set(body["columns"]) == {"sensor", "value"}
    assert body["columns"]["sensor"] == ["a", "a", "a", "b", "b", "b", "c"]
    assert body["totalRows"] == 7
    assert body["returnedRows"] == 7
    assert body["truncated"] is False
    assert body["downsample"] == {"mode": "none"}
    assert body["artifact_id"] == artifact_id


def test_parquet_projection_reads_only_needed_columns(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = pa.table(
        {
            "sensor": ["a", "b", "c"],
            "value": [1.5, 2.5, 3.5],
            "unused": ["u", "v", "w"],
            "flag": [True, False, True],
        }
    )
    artifact_id = env.pin_parquet("sensors.parquet", table)
    seen: list[list[str]] = []
    real_read = engine.pq.read_table

    def spy(source: Any, columns: list[str] | None = None, **kwargs: Any) -> pa.Table:
        seen.append(list(columns or []))
        return real_read(source, columns=columns, **kwargs)

    monkeypatch.setattr(engine.pq, "read_table", spy)

    body = _ok(
        env.query(
            artifact_id,
            {"columns": ["value"], "filter": [{"column": "flag", "op": "eq", "value": True}]},
        )
    )

    assert seen == [["value", "flag"]]
    assert body["columns"] == {"value": [1.5, 3.5]}
    assert body["totalRows"] == 3
    assert body["matchedRows"] == 2


def test_parquet_dictionary_columns_decode(env: _Env) -> None:
    table = pa.table({"sensor": pa.array(["a", "b", "a"]).dictionary_encode(), "v": [1, 2, 3]})
    artifact_id = env.pin_parquet("dict.parquet", table)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "v"],
                "filter": [{"column": "sensor", "op": "eq", "value": "a"}],
            },
        )
    )

    assert body["columns"] == {"sensor": ["a", "a"], "v": [1, 3]}


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_filter_eq(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {"columns": ["t"], "filter": [{"column": "sensor", "op": "eq", "value": "b"}]},
        )
    )

    assert body["columns"]["t"] == [0, 1, 2]
    assert body["matchedRows"] == 3


def test_filter_in_with_mixed_numeric_types(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "t"],
                "filter": [{"column": "t", "op": "in", "value": [0, 2.0]}],
            },
        )
    )

    assert body["columns"]["t"] == [0, 2, 0, 2, 0]


def test_filter_range_inclusive_and_open_sided(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    closed = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["value"],
                "filter": [{"column": "value", "op": "range", "value": [2, 30]}],
            },
        )
    )
    open_low = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["value"],
                "filter": [{"column": "value", "op": "range", "value": [None, 2.0]}],
            },
        )
    )
    open_high = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["value"],
                "filter": [{"column": "value", "op": "range", "value": [30, None]}],
            },
        )
    )

    # NaN never satisfies a range.
    assert closed["columns"]["value"] == [2.0, 3.0, 10.0, 30.0]
    assert open_low["columns"]["value"] == [1.0, 2.0]
    assert open_high["columns"]["value"] == [30.0, 100.0]


def test_filter_isnull_matches_nulls_and_nan(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    nulls = _ok(
        env.query(
            artifact_id,
            {"columns": ["sensor", "t"], "filter": [{"column": "value", "op": "isnull"}]},
        )
    )
    not_null = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor"],
                "filter": [{"column": "note", "op": "isnull", "value": False}],
            },
        )
    )

    assert nulls["columns"] == {"sensor": ["b"], "t": [1]}
    assert not_null["returnedRows"] == 5


def test_filters_are_anded(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "t"],
                "filter": [
                    {"column": "flag", "op": "eq", "value": 1},
                    {"column": "sensor", "op": "in", "value": ["a", "c"]},
                ],
            },
        )
    )

    assert body["columns"] == {"sensor": ["a", "a", "c"], "t": [0, 2, 0]}


def test_filter_contains_matches_case_insensitive_substring(env: _Env) -> None:
    artifact_id = env.pin_csv(
        "notes.csv",
        "sensor,note\na,Alpha Station\nb,beta STATION\nc,Gamma Site\n",
    )

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor"],
                "filter": [{"column": "note", "op": "contains", "value": "station"}],
            },
        )
    )

    assert body["columns"]["sensor"] == ["a", "b"]
    assert body["matchedRows"] == 2


def test_filter_contains_on_a_non_string_column_is_400(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(
            artifact_id,
            {"columns": ["t"], "filter": [{"column": "t", "op": "contains", "value": "1"}]},
        ),
        400,
    )

    assert error["error"] == "invalid_filter_value"


def test_filter_contains_with_an_empty_string_is_a_validation_error(env: _Env) -> None:
    """Matches the shared QueryFilter contract: contains' value has minLength 1."""

    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = env.query(
        artifact_id,
        {"columns": ["sensor"], "filter": [{"column": "note", "op": "contains", "value": ""}]},
    )

    assert _error(response, 422)["error"] == "validation_error"


def test_multiple_filters_on_the_same_column_combine_with_and(env: _Env) -> None:
    """Owner ruling: the UI sends the agent's base dataQuery filters PLUS its
    own, concatenated in one request -- two predicates on the SAME column
    must narrow together (AND), and matchedRows must reflect all of them."""

    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "t"],
                "filter": [
                    {"column": "t", "op": "range", "value": [1, None]},
                    {"column": "t", "op": "range", "value": [None, 2]},
                ],
            },
        )
    )

    assert body["columns"] == {"sensor": ["a", "a", "b", "b"], "t": [1, 2, 1, 2]}
    assert body["matchedRows"] == 4


def test_filter_value_incomparable_with_column_is_400(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(
            artifact_id,
            {"columns": ["t"], "filter": [{"column": "t", "op": "eq", "value": "not-a-number"}]},
        ),
        400,
    )

    assert error["error"] == "invalid_filter_value"
    assert error["details"]["column"] == "t"


def test_malformed_filter_shape_is_validation_error(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = env.query(
        artifact_id,
        {"columns": ["t"], "filter": [{"column": "t", "op": "range", "value": [1]}]},
    )

    assert _error(response, 422)["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def test_group_by_with_every_metric(env: _Env) -> None:
    artifact_id = env.pin_csv(
        "groups.csv",
        "sensor,value\na,1\na,2\na,9\nb,4\nb,\nc,5\nc,7\n",
    )
    metrics = [{"column": "value", "fn": fn} for fn in ("mean", "min", "max", "count", "sum")]
    metrics.append({"column": "value", "fn": "median"})

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "value"],
                "aggregate": {"groupBy": ["sensor"], "metrics": metrics},
            },
        )
    )

    columns = body["columns"]
    assert list(columns) == [
        "sensor",
        "value_mean",
        "value_min",
        "value_max",
        "value_count",
        "value_sum",
        "value_median",
    ]
    assert columns["sensor"] == ["a", "b", "c"]
    assert columns["value_mean"] == [4.0, 4.0, 6.0]
    assert columns["value_min"] == [1, 4, 5]
    assert columns["value_max"] == [9, 4, 7]
    assert columns["value_count"] == [3, 1, 2]
    assert columns["value_sum"] == [12, 4, 12]
    assert columns["value_median"] == [2.0, 4.0, 6.0]
    assert body["totalRows"] == 7
    assert body["matchedRows"] == 3


def test_global_aggregate_after_filter(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["value"],
                "filter": [{"column": "sensor", "op": "eq", "value": "a"}],
                "aggregate": {"metrics": [{"column": "value", "fn": "sum"}]},
            },
        )
    )
    empty = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["value"],
                "filter": [{"column": "sensor", "op": "eq", "value": "zzz"}],
                "aggregate": {
                    "groupBy": [],
                    "metrics": [
                        {"column": "value", "fn": "count"},
                        {"column": "value", "fn": "mean"},
                    ],
                },
            },
        )
    )

    assert body["columns"] == {"value_sum": [6.0]}
    assert empty["columns"] == {"value_count": [0], "value_mean": [None]}


def test_aggregate_mean_over_text_column_is_400(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(
            artifact_id,
            {"columns": ["sensor"], "aggregate": {"metrics": [{"column": "sensor", "fn": "mean"}]}},
        ),
        400,
    )

    assert error["error"] == "invalid_aggregate"


def test_downsample_over_aggregated_output(env: _Env) -> None:
    rows = "".join(f"e{e},{t},{t * (e + 1)}\n" for e in range(3) for t in range(20) for _ in (0, 1))
    artifact_id = env.pin_csv("agg_ds.csv", "entity,t,v\n" + rows)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["entity", "t", "v"],
                "aggregate": {
                    "groupBy": ["entity", "t"],
                    "metrics": [{"column": "v", "fn": "mean"}],
                },
                "downsample": {
                    "mode": "per_entity_lttb",
                    "entityColumn": "entity",
                    "x": "t",
                    "y": "v_mean",
                    "maxPerEntity": 5,
                },
            },
        )
    )

    assert body["matchedRows"] == 60
    assert body["returnedRows"] == 15
    assert body["downsample"]["entities"] == 3


# --------------------------------------------------------------------------- #
# Downsampling
# --------------------------------------------------------------------------- #


def _series_csv(entities: int, points: int) -> str:
    lines = ["entity,t,y"]
    for entity in range(entities):
        for step in range(points):
            lines.append(f"e{entity:03d},{step},{math.sin(step / 5.0) * (entity + 1)}")
    return "\n".join(lines) + "\n"


def test_stride_without_entity_samples_evenly_to_limit(env: _Env) -> None:
    artifact_id = env.pin_csv("series.csv", _series_csv(1, 101))

    body = _ok(
        env.query(artifact_id, {"columns": ["t"], "downsample": {"mode": "stride"}, "limit": 5})
    )

    assert body["columns"]["t"] == [0, 25, 50, 75, 100]
    assert body["truncated"] is False
    assert body["downsample"]["inputRows"] == 101
    assert body["downsample"]["outputRows"] == 5


def test_stride_per_entity_caps_each_entity(env: _Env) -> None:
    artifact_id = env.pin_csv("series.csv", _series_csv(4, 50))

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["entity", "t"],
                "downsample": {"mode": "stride", "entityColumn": "entity", "maxPerEntity": 3},
            },
        )
    )

    assert body["returnedRows"] == 12
    assert body["columns"]["t"] == [0, 24, 49] * 4
    assert body["downsample"]["entities"] == 4


def test_per_entity_lttb_keeps_endpoints_and_respects_cap(env: _Env) -> None:
    artifact_id = env.pin_csv("series.csv", _series_csv(360, 100))

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["entity", "t", "y"],
                "downsample": {
                    "mode": "per_entity_lttb",
                    "entityColumn": "entity",
                    "x": "t",
                    "y": "y",
                    "maxPerEntity": 20,
                },
                "limit": 50_000,
            },
        )
    )

    assert body["totalRows"] == 36_000
    assert body["returnedRows"] == 360 * 20
    assert body["truncated"] is False
    by_entity: dict[str, list[int]] = {}
    for entity, t in zip(body["columns"]["entity"], body["columns"]["t"], strict=True):
        by_entity.setdefault(entity, []).append(t)
    assert len(by_entity) == 360
    for ts in by_entity.values():
        assert len(ts) == 20
        assert ts[0] == 0 and ts[-1] == 99
        assert ts == sorted(ts)
    info = body["downsample"]
    assert info["mode"] == "per_entity_lttb"
    assert info["entities"] == 360
    assert info["maxPerEntity"] == 20
    assert info["inputRows"] == 36_000
    assert info["outputRows"] == 7_200


def test_per_entity_lttb_on_iso_datetime_strings_and_unsorted_rows(env: _Env) -> None:
    lines = ["id,when,v"]
    # Rows arrive shuffled and with mixed zone offsets; LTTB must order by instant.
    lines += [
        "s,2024-01-01T02:00:00+01:00,5",  # 01:00Z
        "s,2024-01-01T00:00:00Z,1",
        "s,2024-01-01T03:00:00Z,2",
        "s,2024-01-01T02:00:00Z,9",
        "s,2024-01-01T04:00:00Z,3",
    ]
    artifact_id = env.pin(
        "times.csv",
        lambda path: path.write_text("\n".join(lines) + "\n", encoding="utf-8"),
    )

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["when", "v"],
                "downsample": {"mode": "per_entity_lttb", "x": "when", "y": "v", "maxPerEntity": 3},
            },
        )
    )

    assert body["returnedRows"] == 3
    assert body["columns"]["v"][0] == 1
    assert body["columns"]["v"][-1] == 3
    assert body["columns"]["v"][1] == 9  # the spike survives
    assert all(isinstance(value, str) for value in body["columns"]["when"])


def test_per_entity_lttb_drops_null_points(env: _Env) -> None:
    artifact_id = env.pin_csv("gaps.csv", "x,y\n0,1\n1,\n2,3\n3,NaN\n4,5\n")

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["x", "y"],
                "downsample": {"mode": "per_entity_lttb", "x": "x", "y": "y", "maxPerEntity": 10},
            },
        )
    )

    assert body["columns"]["x"] == [0, 2, 4]
    assert body["downsample"]["droppedNullRows"] == 2


def test_lttb_requires_numeric_y(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(
            artifact_id,
            {
                "columns": ["t"],
                "downsample": {"mode": "per_entity_lttb", "x": "t", "y": "sensor"},
            },
        ),
        400,
    )

    assert error["error"] == "invalid_downsample"


def test_lttb_without_x_is_validation_error(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = env.query(
        artifact_id, {"columns": ["t"], "downsample": {"mode": "per_entity_lttb", "y": "value"}}
    )

    assert _error(response, 422)["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Limit / caps
# --------------------------------------------------------------------------- #


def test_over_limit_one_shot_query_strides_the_whole_range_not_a_head_slice(
    env: _Env,
) -> None:
    """Owner ruling: ``limit`` is a transfer guard, never a statement about
    intent — a one-shot (no ``offset``) request over the limit gets an even
    stride across the WHOLE range, never a biased first-N slice."""

    artifact_id = env.pin_csv("series.csv", _series_csv(1, 30))

    body = _ok(env.query(artifact_id, {"columns": ["t"], "limit": 10}))

    assert body["returnedRows"] == 10
    assert body["columns"]["t"] == [0, 3, 6, 10, 13, 16, 19, 23, 26, 29]
    # The whole range is represented (spans 0..29, not capped at 9): NOT a
    # naive truncation, so `truncated` stays False — the sampling itself
    # (downsample.reason=over_limit) is how the response says "not everything".
    assert body["truncated"] is False
    assert body["totalRows"] == 30
    assert body["downsample"] == {
        "mode": "stride",
        "reason": "over_limit",
        "inputRows": 30,
        "outputRows": 10,
    }


def test_offset_paging_returns_a_plain_contiguous_page_never_sampled(env: _Env) -> None:
    """The paging twin: an explicit ``offset`` never triggers the over-limit
    stride -- a table pages through the exact, un-sampled row order."""

    artifact_id = env.pin_csv("series.csv", _series_csv(1, 30))

    first_page = _ok(env.query(artifact_id, {"columns": ["t"], "limit": 10, "offset": 0}))
    second_page = _ok(env.query(artifact_id, {"columns": ["t"], "limit": 10, "offset": 10}))
    last_page = _ok(env.query(artifact_id, {"columns": ["t"], "limit": 10, "offset": 25}))

    assert first_page["columns"]["t"] == list(range(0, 10))
    assert first_page["truncated"] is True
    assert first_page["offset"] == 0
    assert first_page["downsample"] == {"mode": "none"}

    assert second_page["columns"]["t"] == list(range(10, 20))
    assert second_page["truncated"] is True
    assert second_page["offset"] == 10

    # The final, short page: 5 rows left (25..29), not padded or sampled.
    assert last_page["columns"]["t"] == list(range(25, 30))
    assert last_page["returnedRows"] == 5
    assert last_page["truncated"] is False
    assert last_page["offset"] == 25


def test_offset_past_the_end_returns_an_empty_page(env: _Env) -> None:
    artifact_id = env.pin_csv("series.csv", _series_csv(1, 30))

    body = _ok(env.query(artifact_id, {"columns": ["t"], "limit": 10, "offset": 1000}))

    assert body["columns"]["t"] == []
    assert body["returnedRows"] == 0
    assert body["truncated"] is False
    assert body["offset"] == 1000


def test_limit_above_ceiling_is_400(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    default_ceiling = _error(env.query(artifact_id, {"columns": ["t"], "limit": 50_001}), 400)
    set_config("artifacts", {"table_query_max_rows": 3})
    configured = _error(env.query(artifact_id, {"columns": ["t"], "limit": 4}), 400)
    default_limit = _ok(env.query(artifact_id, {"columns": ["t"]}))

    assert default_ceiling["error"] == "limit_exceeds_ceiling"
    assert default_ceiling["details"]["max_rows"] == 50_000
    assert configured["details"]["max_rows"] == 3
    # With no explicit limit the default is clamped under the configured
    # ceiling; the 7->3 overflow is an even-stride sample, not a truncation.
    assert default_limit["returnedRows"] == 3
    assert default_limit["truncated"] is False
    assert default_limit["downsample"]["mode"] == "stride"
    assert default_limit["downsample"]["reason"] == "over_limit"


def test_duplicate_columns_is_a_validation_error(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    duplicate = env.query(artifact_id, {"columns": ["t", "t"]})

    assert _error(duplicate, 422)["error"] == "validation_error"


def test_sixty_four_columns_are_accepted(env: _Env) -> None:
    header = ",".join(f"c{index}" for index in range(64))
    row = ",".join(str(index) for index in range(64))
    artifact_id = env.pin_csv("wide.csv", f"{header}\n{row}\n")

    body = _ok(env.query(artifact_id, {"columns": [f"c{index}" for index in range(64)]}))

    assert len(body["columns"]) == 64


def test_more_than_sixty_four_columns_are_accepted(env: _Env) -> None:
    """Owner ruling: no cap that kneecaps intent -- a genuinely wide table's
    columns are all reachable, not refused past an arbitrary count."""

    header = ",".join(f"c{index}" for index in range(100))
    row = ",".join(str(index) for index in range(100))
    artifact_id = env.pin_csv("very_wide.csv", f"{header}\n{row}\n")

    body = _ok(env.query(artifact_id, {"columns": [f"c{index}" for index in range(100)]}))

    assert len(body["columns"]) == 100

    # Omitting columns entirely also returns every one of the 100.
    all_columns = _ok(env.query(artifact_id, {}))
    assert len(all_columns["columns"]) == 100


# --------------------------------------------------------------------------- #
# Omitted columns: "every column", still bounded by limit (owner review fix)
# --------------------------------------------------------------------------- #


def test_omitted_columns_returns_every_column(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(env.query(artifact_id, {}))

    assert set(body["columns"]) == {"sensor", "t", "value", "flag", "note"}
    assert [field["name"] for field in body["schema"]] == [
        "sensor",
        "t",
        "value",
        "flag",
        "note",
    ]
    assert body["returnedRows"] == 7


def test_omitted_columns_still_bounds_rows_by_limit(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(env.query(artifact_id, {"limit": 2}))

    assert set(body["columns"]) == {"sensor", "t", "value", "flag", "note"}
    assert body["returnedRows"] == 2
    # 7 source rows over a limit of 2, no offset/downsample: an even-stride
    # sample, not a naive truncation (see the over-limit stride tests).
    assert body["truncated"] is False
    assert body["downsample"]["mode"] == "stride"


def test_omitted_columns_still_honours_filters(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(env.query(artifact_id, {"filter": [{"column": "sensor", "op": "eq", "value": "a"}]}))

    assert body["matchedRows"] == 3
    assert body["columns"]["sensor"] == ["a", "a", "a"]
    assert set(body["columns"]) == {"sensor", "t", "value", "flag", "note"}


def test_omitted_columns_with_aggregate_is_a_validation_error(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = env.query(
        artifact_id,
        {"aggregate": {"groupBy": ["sensor"], "metrics": [{"column": "value", "fn": "mean"}]}},
    )

    assert _error(response, 422)["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# sort: a table pages through the whole dataset in a caller-chosen order
# --------------------------------------------------------------------------- #


def test_sort_ascending_single_key(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(artifact_id, {"columns": ["sensor", "value"], "sort": [{"column": "value"}]})
    )

    # NaN sorts after real values with pyarrow's default null_placement.
    assert body["columns"]["sensor"][:3] == ["a", "a", "a"]
    assert body["columns"]["value"][0] == 1.0


def test_sort_descending_key(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {"columns": ["sensor", "value"], "sort": [{"column": "value", "desc": True}]},
        )
    )

    assert body["columns"]["sensor"][0] == "c"
    assert body["columns"]["value"][0] == 100.0


def test_sort_compound_keys_apply_in_order(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor", "t"],
                "sort": [{"column": "sensor", "desc": True}, {"column": "t", "desc": True}],
            },
        )
    )

    assert body["columns"]["sensor"] == ["c", "b", "b", "b", "a", "a", "a"]
    assert body["columns"]["t"] == [0, 2, 1, 0, 2, 1, 0]


def test_sort_then_page_returns_the_right_slice_of_the_ordered_rows(env: _Env) -> None:
    artifact_id = env.pin_csv("series.csv", _series_csv(1, 30))

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["t"],
                "sort": [{"column": "t", "desc": True}],
                "limit": 5,
                "offset": 0,
            },
        )
    )

    assert body["columns"]["t"] == [29, 28, 27, 26, 25]
    assert body["truncated"] is True


def test_downsample_runs_before_sort_not_after(env: _Env) -> None:
    """The shared DataQuery contract's own documented order is filter ->
    aggregate -> downsample -> sort -> offset/limit -- downsample picks its
    rows from the UNSORTED (filtered/aggregated) set; the caller's sort then
    orders exactly the rows that made it through. id/value are shuffled so
    picking positions [0, 3, 6, 9] before sorting selects a DIFFERENT row set
    than sorting by value first and then picking those same positions."""

    artifact_id = env.pin_csv(
        "shuffled.csv",
        "id,value\n0,3\n1,7\n2,1\n3,9\n4,4\n5,6\n6,0\n7,8\n8,2\n9,5\n",
    )

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["id", "value"],
                "downsample": {"mode": "stride"},
                "sort": [{"column": "value"}],
                "limit": 4,
            },
        )
    )

    assert body["columns"]["id"] == [6, 0, 9, 3]
    assert body["columns"]["value"] == [0, 3, 5, 9]


def test_sort_unknown_column_is_columns_not_found(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(artifact_id, {"columns": ["sensor"], "sort": [{"column": "nope"}]}), 400
    )

    assert error["error"] == "columns_not_found"
    assert error["details"]["missing"] == ["nope"]


def test_sort_by_an_aggregate_output_column(env: _Env) -> None:
    """With aggregate set, sort targets the AGGREGATE'S OWN output columns
    (e.g. a metric's ``{column}_{fn}`` name), not raw source columns."""

    artifact_id = env.pin_csv(
        "readings.csv",
        "sensor,t\na,5\na,1\nb,10\nb,2\nc,3\n",
    )

    body = _ok(
        env.query(
            artifact_id,
            {
                "columns": ["sensor"],
                "aggregate": {
                    "groupBy": ["sensor"],
                    "metrics": [{"column": "t", "fn": "max"}],
                },
                "sort": [{"column": "t_max", "desc": True}],
            },
        )
    )

    # Per-sensor max(t): b=10, a=5, c=3 -- strictly descending, no ties.
    assert body["columns"]["sensor"] == ["b", "a", "c"]


def test_oversized_source_is_413(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    set_config("artifacts", {"table_query_max_source_bytes": 16})

    error = _error(env.query(artifact_id, {"columns": ["t"]}), 413)

    assert error["error"] == "artifact_too_large"
    assert error["details"]["max_bytes"] == 16


def test_unknown_format_field_is_validation_error(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = env.query(artifact_id, {"columns": ["t"], "format": "arrow"})

    assert _error(response, 422)["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# Errors / security
# --------------------------------------------------------------------------- #


def test_bad_column_names_are_400_with_names(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(
            artifact_id,
            {
                "columns": ["t", "nope"],
                "filter": [{"column": "ghost", "op": "isnull"}],
            },
        ),
        400,
    )

    assert error["error"] == "columns_not_found"
    assert error["details"]["missing"] == ["nope", "ghost"]
    assert "sensor" in error["details"]["available"]


def test_downsample_column_missing_after_aggregate_is_400(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    error = _error(
        env.query(
            artifact_id,
            {
                "columns": ["sensor"],
                "aggregate": {"groupBy": ["sensor"], "metrics": [{"column": "value", "fn": "max"}]},
                "downsample": {"mode": "per_entity_lttb", "x": "t", "y": "value_max"},
            },
        ),
        400,
    )

    assert error["error"] == "columns_not_found"
    assert error["details"]["missing"] == ["t"]


def test_unknown_artifact_is_404(env: _Env) -> None:
    error = _error(env.query("art_does_not_exist", {"columns": ["t"]}), 404)

    assert error["error"] == "not_found"


def test_non_tabular_artifact_is_415(env: _Env) -> None:
    artifact_id = env.pin_csv("notes.txt", "a,b\n1,2\n")

    error = _error(env.query(artifact_id, {"columns": ["a"]}), 415)

    assert error["error"] == "unsupported_media_type"


def test_corrupt_parquet_is_422(env: _Env) -> None:
    artifact_id = env.pin("broken.parquet", lambda path: path.write_bytes(b"not parquet"))

    error = _error(env.query(artifact_id, {"columns": ["a"]}), 422)

    assert error["error"] == "table_read_failed"


def test_filesystem_path_is_not_an_artifact_id(env: _Env) -> None:
    source = env.root / "loose.csv"
    source.write_text("a\n1\n", encoding="utf-8")

    response = env.client.post(
        "/v1/artifacts/table-query",
        json={"columns": ["a"], "path": str(source)},
    )
    by_path_id = env.query(str(source).replace("\\", "/").lstrip("/"), {"columns": ["a"]})

    assert response.status_code in (404, 405)
    assert by_path_id.status_code == 404


def test_tampered_bytes_are_an_integrity_violation(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    original = (env.root / "sensors.csv").read_bytes()
    tampered = original.replace(b"100.0", b"999.0")
    # Rewrite every copy of the bytes (workspace file and any CAS blob).
    copies = [p for p in env.root.rglob("*") if p.is_file() and p.read_bytes() == original]
    assert copies
    for path in copies:
        path.write_bytes(tampered)

    error = _error(env.query(artifact_id, {"columns": ["value"]}), 409)

    assert error["error"] == "integrity_violation"


# --------------------------------------------------------------------------- #
# Serialisation, cache, timeout
# --------------------------------------------------------------------------- #


def test_nan_and_datetimes_serialize_json_safe(env: _Env) -> None:
    table = pa.table(
        {
            "when": pa.array(
                [1_700_000_000_123_456_789, None, 0],
                type=pa.timestamp("ns", tz="UTC"),
            ),
            "day": pa.array([19_000, 19_001, None], type=pa.date32()),
            "v": [float("nan"), float("inf"), 1.25],
        }
    )
    artifact_id = env.pin_parquet("times.parquet", table)

    response = env.query(artifact_id, {"columns": ["when", "day", "v"]})

    assert "NaN" not in response.text and "Infinity" not in response.text
    body = _ok(response)
    assert body["columns"]["v"] == [None, None, 1.25]
    assert body["columns"]["when"] == [
        "2023-11-14T22:13:20.123456+00:00",
        None,
        "1970-01-01T00:00:00+00:00",
    ]
    assert body["columns"]["day"] == ["2022-01-08", "2022-01-09", None]
    assert body["schema"][0]["type"] == "timestamp[ns, tz=UTC]"


def test_csv_timestamps_serialize_as_iso(env: _Env) -> None:
    artifact_id = env.pin_csv("ts.csv", "when,v\n2024-03-01T12:00:00,1\n2024-03-02T00:30:00,NaN\n")

    body = _ok(env.query(artifact_id, {"columns": ["when", "v"]}))

    assert body["columns"] == {
        "when": ["2024-03-01T12:00:00", "2024-03-02T00:30:00"],
        "v": [1.0, None],
    }


def test_identical_query_is_served_from_cache(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    calls: list[int] = []
    real_run = engine.run_table_query

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(engine, "run_table_query", counting)
    query = {"columns": ["sensor"], "filter": [{"column": "t", "op": "eq", "value": 0}]}

    first = _ok(env.query(artifact_id, query))
    second = _ok(env.query(artifact_id, dict(reversed(list(query.items())))))
    other = _ok(env.query(artifact_id, {**query, "limit": 2}))

    assert first["cached"] is False
    assert second["cached"] is True
    assert other["cached"] is False
    assert {k: v for k, v in first.items() if k != "cached"} == {
        k: v for k, v in second.items() if k != "cached"
    }
    assert len(calls) == 2


def test_cache_can_be_disabled(env: _Env) -> None:
    set_config("artifacts", {"table_query_cache_entries": 0})
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    env.query(artifact_id, {"columns": ["t"]})
    again = _ok(env.query(artifact_id, {"columns": ["t"]}))

    assert again["cached"] is False


def test_cache_is_bounded_lru() -> None:
    cache = route.TableQueryCache()
    for index in range(5):
        cache.put(("sha", "csv", str(index)), {"returnedRows": 1, "columns": {"a": [index]}}, 3)

    assert len(cache) == 3
    assert cache.get(("sha", "csv", "0")) is None
    assert cache.get(("sha", "csv", "4")) is not None
    huge = {"returnedRows": 1_000_000, "columns": {"a": []}}
    cache.put(("sha", "csv", "huge"), huge, 3)
    assert cache.get(("sha", "csv", "huge")) is None


def test_wall_clock_timeout_is_504(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    set_config("artifacts", {"table_query_timeout_s": 0.2})

    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        time.sleep(1.0)
        return {}

    monkeypatch.setattr(engine, "run_table_query", slow)

    error = _error(env.query(artifact_id, {"columns": ["t"]}), 504)

    assert error["error"] == "table_query_timeout"
    assert error["details"]["timeout_s"] == 0.2


def test_engine_deadline_stops_between_stages(tmp_path: Path) -> None:
    source = tmp_path / "s.csv"
    source.write_text(_SENSORS_CSV, encoding="utf-8")
    request = engine.TableQueryRequest.model_validate({"columns": ["t"]})

    with pytest.raises(engine.TableQueryTimeout):
        engine.run_table_query(
            source, "csv", request, limit=10, deadline=time.monotonic() - 1, timeout_s=1.0
        )
