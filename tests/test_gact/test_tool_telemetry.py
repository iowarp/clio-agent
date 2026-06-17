"""CLIO-BBBBBBBBBB18: tool.call.started + tool.call.completed events
are live lifecycle telemetry, not reconstructed from post-turn summaries.

Reads from EventBus history after the POST instead of streaming SSE
— TestClient deadlocks on unbounded SSE responses (same story as
test_sse.py). The bus history is what the endpoint replays anyway,
so testing it directly is both faithful and deadlock-free.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _settled_history(
    app, sid: str, *, timeout: float = 5.0, stable_window: float = 0.12, poll: float = 0.02
):
    """Return the session's bus history once it has stopped growing.

    The live tool observer publishes ``tool.call.*`` events onto the event loop
    from the daemon turn thread, so they can land slightly AFTER ``complete_turn``
    sees the assistant message settle. Reading the history once immediately races
    that flush. Poll until the history length is stable for a short window (or the
    timeout fires) so the assertions see the fully-flushed event stream.
    """

    deadline = time.monotonic() + timeout
    last_n: int = -1
    stable_start: float | None = None
    while time.monotonic() < deadline:
        history = app.state.bus._history.get(sid, [])
        n = len(history)
        if n > 0 and n == last_n:
            if stable_start is None:
                stable_start = time.monotonic()
            elif time.monotonic() - stable_start >= stable_window:
                return history
        else:
            stable_start = None
            last_n = n
        time.sleep(poll)
    return app.state.bus._history.get(sid, [])


from clio_agent.gact.app import (
    _bounded_tool_call_result,
    _is_bounded_tool_result,
    _make_tool_observer,
    _merge_tool_call_rows,
    _propose_edit_diffs_from_pred,
    build_app,
)


class _FakePred:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_propose_edit_promotes_tool_result_to_file_diff() -> None:
    # iowarp/clio-agent#674: a dynamic tool agent's fs_propose_edit result must
    # be promoted into a file-diff proposal (it never lands in pred.file_diffs).
    pred = _FakePred(
        tools_called=[
            {"name": "fs_read_file", "ok": True, "result": {"content": "x"}},
            {
                "name": "fs_propose_edit",
                "ok": True,
                "result": {
                    "path": "handlers.go",
                    "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                    "new_content": "new\n",
                    "lines_added": 1,
                    "lines_removed": 1,
                },
            },
        ]
    )
    diffs = _propose_edit_diffs_from_pred(pred)
    assert len(diffs) == 1
    assert diffs[0]["path"] == "handlers.go"
    assert diffs[0]["unified_diff"].startswith("@@")
    assert diffs[0]["new_content"] == "new\n"
    assert diffs[0]["lines_added"] == 1


def test_propose_edit_skips_failed_calls_and_dedups() -> None:
    pred = _FakePred(
        tools_called=[
            {
                "name": "fs_propose_edit",
                "ok": False,
                "result": {"path": "a.py", "unified_diff": "d"},
            },
            {
                "name": "fs_propose_edit",
                "ok": True,
                "result": {"path": "b.py", "unified_diff": "x"},
            },
            {
                "name": "fs_propose_edit",
                "ok": True,
                "result": {"path": "b.py", "unified_diff": "x"},
            },
        ]
    )
    diffs = _propose_edit_diffs_from_pred(pred)
    assert [d["path"] for d in diffs] == ["b.py"]  # failed dropped, dup collapsed


def test_propose_edit_falls_back_to_trajectory() -> None:
    pred = _FakePred(
        tools_called=[],
        trajectory={
            "tool_name_0": "fs_propose_edit",
            "tool_args_0": {"path": "c.py"},
            "observation_0": {"path": "c.py", "unified_diff": "@@ x @@", "new_content": "c"},
        },
    )
    diffs = _propose_edit_diffs_from_pred(pred)
    assert len(diffs) == 1 and diffs[0]["path"] == "c.py"


@pytest.fixture(autouse=True)
def _isolate_tool_runtime_globals():
    """Reset the process-global tool-runtime hooks around every test.

    Turns run in daemon threads and the tool observer/gate/etc. are process
    globals that ``build_app(agent=...)`` installs eagerly but that are cleared
    only on lifespan shutdown. These tests use a bare ``TestClient`` (no ``with``),
    so without an explicit reset a prior test's lingering daemon turn thread can
    fire the global observer AFTER the next test has re-pointed it, leaking
    events into the wrong app's bus and making the live-observer assertions flake
    nondeterministically. Clearing to None before and after each test makes a
    late stray call a no-op, so each test's own ``build_app`` install is the
    only authority during its turn.
    """
    from clio_agent.tools import execution  # noqa: PLC0415

    def _clear() -> None:
        execution.set_global_tool_observer(None)
        execution.set_global_permission_gate(None)
        execution.set_global_cancellation_checker(None)
        execution.set_global_tool_interceptor(None)

    _clear()
    yield
    _clear()


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    tools_called: list = None  # type: ignore[assignment]


class _Agent:
    def forward(self, question: str, session_id: str):
        return _Pred(
            tools_called=[
                {"name": "hdf5.analyze", "ok": True, "duration_ms": 14.0, "cached": False},
                {"name": "parquet.summarise", "ok": True, "duration_ms": 22.3, "cached": True},
            ]
        )


class _LiveObservedAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", {"filepath": "x.h5"}, "started", None)
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", {"filepath": "x.h5"}, "completed", None)
        return _Pred()


class _LiveObservedWithPosthocTraceAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        args = {"filepath": "x.h5"}
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "started", None)
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "completed", None)
        return _Pred(
            tools_called=[
                {
                    "name": "hdf5_list_datasets",
                    "args": args,
                    "ok": True,
                    "duration_ms": 999.0,
                    "cached": True,
                    "result": {"datasets": ["safe_float"], "checksum": "abc123"},
                }
            ]
        )


class _LiveObservedResultAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        args = {"filepath": "x.h5"}
        result = {"datasets": ["safe_float"], "checksum": "abc123"}
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "started", None)
        _GLOBAL_TOOL_OBSERVER("hdf5_list_datasets", args, "completed", None, result)
        return _Pred()


class _LiveObservedStructuredErrorResultAgent:
    def forward(self, question: str, session_id: str):
        from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

        assert _GLOBAL_TOOL_OBSERVER is not None
        args = {"output_path": "/missing/plot.png"}
        result = {
            "error": {
                "type": "file_policy",
                "code": "parent_not_found",
                "message": "Output directory does not exist",
            }
        }
        _GLOBAL_TOOL_OBSERVER("ndp_plot_csv_timeseries", args, "started", None)
        _GLOBAL_TOOL_OBSERVER(
            "ndp_plot_csv_timeseries",
            args,
            "completed",
            "parent_not_found: Output directory does not exist",
            result,
        )
        return _Pred()


class _ToolRoutingAgent:
    def _selected_expert_for_tool(self, tool_name: str) -> str:
        assert tool_name == "NdpSearchDatasets"
        return "ndp_catalog"

    def _parent_route_for_child(self, expert_id: str) -> str:
        assert expert_id == "ndp_catalog"
        return "data"


@pytest.fixture()
def app_client(tmp_path: Path):
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    return app, client


def test_bounded_tool_result_is_idempotent_no_nested_preview() -> None:
    # A bounded result flows across stages (tool -> catalog -> data -> main) and was
    # re-bounded each hop, nesting preview-of-preview (observed: a geo_filter result
    # wrapped 22x by turn.completed -> data buried -> staged station unverifiable).
    big = {
        "ok": True,
        "count": 144,
        "points": [{"Site": f"S{i}", "lat": 47.6, "lon": -122.3} for i in range(1200)],
    }
    b1 = _bounded_tool_call_result(big)
    assert _is_bounded_tool_result(b1)  # got bounded (was > limit)
    b2 = _bounded_tool_call_result(b1)
    assert b2 is b1  # idempotent: already-bounded payload is never re-wrapped
    assert json.dumps(b2).count("original_chars") == 1  # exactly one preview layer
    # A small (unbounded) result passes through untouched.
    small = {"ok": True, "count": 1}
    assert _bounded_tool_call_result(small) == small
    assert not _is_bounded_tool_result(small)


def test_posthoc_tools_called_metadata_does_not_emit_lifecycle_events(app_client) -> None:
    from .conftest import complete_turn

    app, client = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = _settled_history(app, sid)
    started = [e for e in history if e.type == "tool.call.started"]
    completed = [e for e in history if e.type == "tool.call.completed"]

    assert started == []
    assert completed == []
    assert assistant["metadata"]["tools_called"] == [
        {
            "name": "hdf5.analyze",
            "ok": True,
            "duration_ms": 14.0,
            "cached": False,
            "telemetry_source": "posthoc_prediction",
        },
        {
            "name": "parquet.summarise",
            "ok": True,
            "duration_ms": 22.3,
            "cached": True,
            "telemetry_source": "posthoc_prediction",
        },
    ]


def test_live_observed_tool_call_is_not_reemitted_post_turn(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(sessions_path=tmp_path / "s.json", agent=_LiveObservedAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = _settled_history(app, sid)
    started = [e for e in history if e.type == "tool.call.started"]
    completed = [e for e in history if e.type == "tool.call.completed"]

    assert [e.payload["tool"] for e in started] == ["hdf5_list_datasets"]
    assert [e.payload["tool"] for e in completed] == ["hdf5_list_datasets"]
    assert started[0].payload["telemetry_source"] == "live_observer"
    assert completed[0].payload["telemetry_source"] == "live_observer"
    assert completed[0].payload["ui_summary"] == "Tool hdf5_list_datasets completed."
    assert completed[0].payload["result_summary"] == "Tool hdf5_list_datasets completed."
    assert assistant["metadata"]["tools_called"][0]["name"] == "hdf5_list_datasets"
    assert assistant["metadata"]["tools_called"][0]["args"] == {"filepath": "x.h5"}
    assert assistant["metadata"]["tools_called"][0]["telemetry_source"] == "live_observer"


def test_live_observer_upgrades_matching_posthoc_trace_metadata(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedWithPosthocTraceAgent(),
    )
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = _settled_history(app, sid)
    started = [e for e in history if e.type == "tool.call.started"]
    completed = [e for e in history if e.type == "tool.call.completed"]
    tools_called = assistant["metadata"]["tools_called"]

    assert len(started) == 1
    assert len(completed) == 1
    assert tools_called[0]["name"] == "hdf5_list_datasets"
    assert tools_called[0]["telemetry_source"] == "live_observer"
    assert tools_called[0]["duration_ms"] != 999.0
    assert tools_called[0]["cached"] is False
    assert tools_called[0]["result"] == {"datasets": ["safe_float"], "checksum": "abc123"}


def test_live_observer_records_completed_tool_result_evidence(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedResultAgent(),
    )
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = _settled_history(app, sid)
    completed = [e for e in history if e.type == "tool.call.completed"]
    tools_called = assistant["metadata"]["tools_called"]
    tool_results = [
        e.payload["part"]
        for e in history
        if e.type == "message.part.added" and e.payload.get("part", {}).get("type") == "tool_result"
    ]

    assert completed[0].payload["result"] == {
        "datasets": ["safe_float"],
        "checksum": "abc123",
    }
    assert tools_called[0]["result"] == {
        "datasets": ["safe_float"],
        "checksum": "abc123",
    }
    assert tool_results[0]["metadata"]["result"] == {
        "datasets": ["safe_float"],
        "checksum": "abc123",
    }


def test_live_observer_preserves_failed_structured_tool_result_evidence(tmp_path: Path) -> None:
    from .conftest import complete_turn

    app = build_app(
        sessions_path=tmp_path / "s.json",
        agent=_LiveObservedStructuredErrorResultAgent(),
    )
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "analyze")

    history = _settled_history(app, sid)
    completed = [e for e in history if e.type == "tool.call.completed"]
    tools_called = assistant["metadata"]["tools_called"]
    tool_results = [
        e.payload["part"]
        for e in history
        if e.type == "message.part.added" and e.payload.get("part", {}).get("type") == "tool_result"
    ]

    expected_result = {
        "error": {
            "type": "file_policy",
            "code": "parent_not_found",
            "message": "Output directory does not exist",
        }
    }
    assert completed[0].payload["ok"] is False
    assert completed[0].payload["error"] == "parent_not_found: Output directory does not exist"
    assert completed[0].payload["result"] == expected_result
    assert tools_called[0]["ok"] is False
    assert tools_called[0]["error"] == "parent_not_found: Output directory does not exist"
    assert tools_called[0]["result"] == expected_result
    assert tool_results[0]["is_error"] is True
    assert tool_results[0]["metadata"]["result"] == expected_result


def test_tool_call_merge_does_not_attach_success_result_to_failed_attempt() -> None:
    rows = _merge_tool_call_rows(
        [
            {
                "name": "ndp_get_dataset_details",
                "call_id": "call_failed",
                "args": {"dataset_identifier": "abc"},
                "ok": False,
                "error": "ClosedResourceError()",
                "telemetry_source": "live_observer",
            }
        ],
        [
            {
                "name": "ndp_get_dataset_details",
                "args": {"dataset_identifier": "abc"},
                "ok": True,
                "result": {"dataset": {"id": "abc", "title": "EarthScope Stations Dataset"}},
            }
        ],
    )

    assert len(rows) == 2
    assert rows[0]["ok"] is False
    assert "result" not in rows[0]
    assert rows[1]["ok"] is True
    assert rows[1]["result"] == {"dataset": {"id": "abc", "title": "EarthScope Stations Dataset"}}


def test_live_tool_observer_emits_route_context_before_tool_part(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_ToolRoutingAgent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    observer = _make_tool_observer(app)

    observer("NdpSearchDatasets", {"search_terms": "seismic"}, "started", None)

    history = _settled_history(app, sid)
    added_parts = [e.payload["part"] for e in history if e.type == "message.part.added"]
    assert [p["type"] for p in added_parts] == [
        "routing_decision",
        "expert_handoff",
        "tool_call",
    ]
    assert added_parts[0]["selected_agent"] == "data"
    assert added_parts[0]["execution_path"] == "orchestrator -> data"
    assert added_parts[1]["metadata"]["agent_id"] == "ndp_catalog"
    assert added_parts[1]["metadata"]["parent_id"] == "data"
    assert added_parts[2]["tool_name"] == "NdpSearchDatasets"


def test_tool_result_full_in_trace_but_bounded_in_ledger(tmp_path: Path, monkeypatch) -> None:
    """T1: the canonical trace keeps the FULL tool result (never capped) while the
    ledger/assistant-metadata projection stays bounded. Drives the observer
    DIRECTLY (no full turn) under the off-loop file backend, then reads the trace."""
    import json as _json

    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "file")
    monkeypatch.setenv("CLIO_SEMANTIC_TRACE_PATH", str(trace_dir))
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    observer = _make_tool_observer(app)
    big_result = {"rows": [f"station_{i:05d}" for i in range(4000)]}  # >>12000 chars
    observer("hdf5_dump_all", {"filepath": "x.h5"}, "started", None)
    observer("hdf5_dump_all", {"filepath": "x.h5"}, "completed", None, big_result)

    # T1: durable trace carries the FULL result (all 4000 rows, not a preview).
    app.state.semantic_trace_backend.flush()
    rows = [
        _json.loads(line) for line in (trace_dir / f"{sid}.semantic.jsonl").read_text().splitlines()
    ]
    tcc = next(r for r in rows if r["event_type"] == "tool.call.completed")
    assert isinstance(tcc["payload"]["result"], dict)
    assert len(tcc["payload"]["result"]["rows"]) == 4000
    assert tcc["payload"]["result"].get("truncated") is not True

    # ...while the per-session ledger (assistant-metadata projection) is BOUNDED.
    led = app.state.tool_call_ledger.get(sid, [])
    assert led and isinstance(led[0]["result"], dict) and led[0]["result"].get("truncated") is True
