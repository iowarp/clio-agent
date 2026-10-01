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

import asyncio
import csv
import io
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
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


def test_export_route_registration_imports_the_export_engine_eagerly() -> None:
    """#1551 review item 6: the same numpy-import-race fix as table-query's
    own route -- see that test's docstring for the mechanism."""

    import sys

    from fastapi import FastAPI

    from clio_agent.gact.routes import artifact_table_export as route

    # Restored in `finally` -- see the matching test in
    # `test_artifact_table_query.py` for why a bare pop-and-reimport would
    # pollute the rest of the test session.
    module_name = "clio_agent.gact.artifacts.table_export"
    original = sys.modules.pop(module_name, None)
    try:
        assert module_name not in sys.modules

        app = FastAPI()
        route.register_artifact_table_export_routes(app)

        assert module_name in sys.modules
    finally:
        if original is not None:
            sys.modules[module_name] = original
        else:
            sys.modules.pop(module_name, None)


def test_export_unknown_artifact_is_404(env: _Env) -> None:
    response = _export(env, "artifact_missing", {"scope": "current", "format": "csv"})
    assert response.status_code == 404, response.text


def test_export_unsupported_format_media_type_for_non_tabular_artifact(env: _Env) -> None:
    artifact_id = env.pin("notes.txt", lambda path: path.write_text("hello", encoding="utf-8"))

    response = _export(env, artifact_id, {"scope": "current", "format": "csv"})

    assert response.status_code == 415, response.text


def test_export_too_large_refuses_even_with_an_explicit_sort(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1551 review item 2: `table_query.py`'s over-limit guard used to gate
    `export_too_large` on `not request.sort`, so a SORTED current-view export
    past the ceiling flowed through completely unbounded -- a sort+limit
    query is a legitimate top-/bottom-N request for interactive table-query
    (which still slices to `limit` afterward), but an export is never sliced
    afterward at all."""

    from clio_agent.gact.routes import artifact_table_export as route

    monkeypatch.setattr(route, "table_export_max_rows", lambda: 2)
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env,
        artifact_id,
        {"columns": ["sensor", "value"], "sort": [{"column": "value"}], "scope": "current", "format": "csv"},
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["error"] == "export_too_large"


def test_full_scope_export_too_large_also_refuses(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1551 review item 2: `_full_scope_result` never checked `max_rows` at
    all -- "full" means "ignore the view's own filter," never "ignore the row
    ceiling.\""""

    from clio_agent.gact.routes import artifact_table_export as route

    monkeypatch.setattr(route, "table_export_max_rows", lambda: 2)
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env, artifact_id, {"columns": ["sensor"], "scope": "full", "format": "csv"}
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["error"] == "export_too_large"


def test_full_scope_columns_not_found_is_a_typed_400(env: _Env) -> None:
    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)

    response = _export(
        env, artifact_id, {"columns": ["not_a_real_column"], "scope": "full", "format": "csv"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["error"] == "columns_not_found"


def test_parquet_source_artifact_exports_to_csv(env: _Env) -> None:
    """The export engine reads a Parquet SOURCE artifact just as readily as a
    CSV one -- `table_format_for` dispatches on the artifact's own name."""

    table = pa.table({"sensor": ["a", "b", "c"], "value": [1.0, 2.0, 3.0]})
    artifact_id = env.pin("sensors.parquet", lambda path: pq.write_table(table, path))

    response = _export(env, artifact_id, {"scope": "full", "format": "csv"})

    assert response.status_code == 200, response.text
    rows = _rows_from_csv(response.content)
    assert [row["sensor"] for row in rows] == ["a", "b", "c"]


def test_non_latin1_filename_round_trips_through_content_disposition(env: _Env) -> None:
    """#1551 review item 4: a non-latin-1 artifact name used to 500 deep
    inside header encoding -- HTTP header values are latin-1 only."""

    artifact_id = env.pin_csv("数据.csv", _SENSORS_CSV)

    response = _export(env, artifact_id, {"scope": "current", "format": "csv"})

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert 'filename="' in disposition  # an ASCII fallback is always present
    assert "filename*=UTF-8''%E6%95%B0%E6%8D%AE" in disposition


def test_filename_with_a_literal_quote_never_breaks_the_header() -> None:
    """A literal double quote in a filename must not break the quoted-string
    syntax (nor crash) -- #1551 review item 4. A real double-quote character
    is not a legal Windows path, so this exercises the header builder
    directly rather than round-tripping an artifact with that literal name."""

    from clio_agent.gact.routes.content_disposition import content_disposition

    disposition = content_disposition('weird"name.csv')

    # Exactly the opening/closing quote of the ASCII fallback's filename=
    # value -- the embedded quote was replaced, never left to break the
    # quoted-string syntax.
    assert disposition.count('"') == 2
    assert 'filename="weird_name.csv"' in disposition
    assert "filename*=UTF-8''weird%22name.csv" in disposition


def test_export_stream_stops_once_cancelled(env: _Env) -> None:
    """#1551 review item 3: the streaming serializer checks cancellation
    between batches, so an abandoned download stops promptly instead of
    finishing unread work."""

    from clio_agent.gact.artifacts import table_export
    from clio_agent.gact.artifacts import table_query as engine
    from clio_agent.gact.artifacts.table_query_models import TableQueryRequest

    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    app = env.client.app
    record, version = app.state.artifact_registry.get_by_artifact_id(artifact_id)
    from clio_agent.gact.routes.table_route_shared import table_source

    source = table_source(app, record, version)

    already_cancelled = threading.Event()
    already_cancelled.set()
    cancellation = engine.QueryCancellation(
        deadline=time.monotonic() + 60, timeout_s=60, cancel_event=already_cancelled
    )
    result = table_export.resolve_export_table(
        source,
        "csv",
        TableQueryRequest(columns=["sensor", "value"]),
        scope="current",
        max_rows=1_000_000,
        cancellation=engine.QueryCancellation(deadline=time.monotonic() + 60, timeout_s=60),
    )

    with pytest.raises(engine.TableQueryCancelled):
        list(table_export.stream_export(result, "csv", cancellation=cancellation))


def test_concurrent_exports_are_bounded_by_the_shared_semaphore(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1551 review item 3: exports reuse table-query's own concurrency
    semaphore -- a burst of exports waits its turn rather than running fully
    concurrently without bound."""

    from clio_agent.gact.routes import artifact_table_export as route

    # The SAME semaphore instance every call -- the real
    # `_concurrency_semaphore_for` caches one on `app.state`; a lambda that
    # built a fresh `Semaphore(1)` per call would give each concurrent
    # request its own uncontended semaphore, proving nothing.
    shared_semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(route, "_concurrency_semaphore_for", lambda app: shared_semaphore)

    artifact_id = env.pin_csv("sensors.csv", _SENSORS_CSV)
    active = 0
    peak = 0
    lock = threading.Lock()

    real_resolve = route._resolve_export

    def _tracking_resolve(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.2)
            return real_resolve(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(route, "_resolve_export", _tracking_resolve)

    async def _run() -> None:
        transport = httpx.ASGITransport(app=env.client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            body = {"scope": "current", "format": "csv"}
            responses = await asyncio.gather(
                client.post(f"/v1/artifacts/{artifact_id}/table-export", json=body),
                client.post(f"/v1/artifacts/{artifact_id}/table-export", json=body),
            )
            for response in responses:
                assert response.status_code == 200, response.text

    asyncio.run(_run())
    assert peak == 1
