from __future__ import annotations

from pathlib import Path

from scripts import run_demo_benchmark as bench


def _message(
    *,
    text: str = "ok",
    error: str | None = None,
    stream_source: str = "batch",
    route_source: str = "dspy",
    tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    error_info = None
    if error is not None:
        error_info = {"error": error, "message": error, "details": {}, "recoverable": True}
    return {
        "parts": [
            {
                "type": "routing_decision",
                "selected_agent": "analysis",
                "metadata": {"route_source": route_source},
            },
            {"type": "text", "text": text},
        ],
        "metadata": {
            "stream_source": stream_source,
            "stream_fallback": {"reason": "provider_streaming_unsupported"},
            "tools_called": tools or [],
            "expert_handoffs": [],
        },
        "error_info": error_info,
        "stop_reason": "cancelled" if error == "cancelled" else "end_turn",
    }


def _result(case_id: str, *, passed: bool = True) -> bench.DemoResult:
    expects_error = case_id == "missing_hdf5_error"
    expects_cancelled = case_id == "claude_cancellation_surface"
    message = _message(
        text="" if expects_error or expects_cancelled else "ok",
        error="cancelled" if expects_cancelled else ("tool_error" if expects_error else None),
        tools=[{"name": "hdf5_list_datasets"}] if "hdf5" in case_id else [],
    )
    if not passed:
        message = _message(text="", error="provider_error")
    return bench.DemoResult(
        case=bench.DemoCase(
            case_id=case_id,
            title=case_id,
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expects_error=expects_error,
            expects_cancelled=expects_cancelled,
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "claude_code", "model": "sonnet", "api_base": ""},
        benchmark_lane="claude_code",
    )


def test_select_claude_code_lane_cases() -> None:
    cases = [
        bench.DemoCase(
            case_id=case_id,
            title=case_id,
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        )
        for case_id in (
            "workflow_hdf5_overview",
            "workflow_parquet_profile",
            "reasoning_cross_file_triage_nanoagents",
            "missing_hdf5_error",
            "claude_cancellation_surface",
            "provider_swap_memory_followup",
        )
    ]

    selected, missing = bench._select_cases(cases, lane="claude_code", case_ids=())

    assert missing == []
    assert [case.case_id for case in selected] == list(bench._BENCHMARK_LANES["claude_code"])


def test_select_real_orchestrator_lane_cases() -> None:
    cases = [
        bench.DemoCase(
            case_id=case_id,
            title=case_id,
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        )
        for case_id in (
            "reasoning_cross_file_triage_nanoagents",
            "cross_file_dirty_quality_gate_nanoagents",
            "csv_status_visual_summary",
            "dirty_quality_dashboard_multi_turn",
            "genomics_reference_variant_review",
            "materials_cif_structure_review",
            "geospatial_field_site_review",
            "microscopy_png_readiness_review",
            "mass_spec_mzml_qc_review",
            "ndp_catalog_discovery",
            "ndp_seismic_waveform_to_plot",
            "reasoning_adios_bp5_container",
            "workflow_hdf5_overview",
        )
    ]

    selected, missing = bench._select_cases(cases, lane="real_orchestrator", case_ids=())

    assert missing == []
    assert [case.case_id for case in selected] == list(
        bench._BENCHMARK_LANES["real_orchestrator"]
    )


def test_select_semantic_regression_lane_cases() -> None:
    cases = [
        bench.DemoCase(
            case_id=case_id,
            title=case_id,
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        )
        for case_id in (
            "reasoning_cross_file_triage_nanoagents",
            "ndp_seismic_waveform_to_plot",
            "marketplace_seismic_waveform_review",
            "marketplace_mcp_calculator_scope",
            "provider_swap_memory_followup",
        )
    ]

    selected, missing = bench._select_cases(cases, lane="semantic_regression", case_ids=())

    assert missing == []
    assert [case.case_id for case in selected] == list(
        bench._BENCHMARK_LANES["semantic_regression"]
    )


def test_real_orchestrator_is_run_benchmark_default_lane() -> None:
    assert bench.run_benchmark.__kwdefaults__["lane"] == "real_orchestrator"


def test_real_orchestrator_turns_pin_root_agent() -> None:
    case = bench.DemoCase(
        case_id="ndp_catalog_discovery",
        title="ndp",
        category="test",
        prompt="Find datasets",
        why="why",
        expected="expected",
        session_group="test",
    )
    explicit = bench.DemoCase(
        case_id="direct_data_case",
        title="data",
        category="test",
        prompt="Inspect file",
        why="why",
        expected="expected",
        session_group="test",
        turn_agent_id="data",
    )

    assert bench._turn_agent_id_for_lane(case, "real_orchestrator") == "main"
    assert bench._turn_agent_id_for_lane(case, "all") == "main"
    assert bench._turn_agent_id_for_lane(case, "claude_code") == "main"
    assert bench._turn_agent_id_for_lane(explicit, "real_orchestrator") == "data"


def test_all_lane_keeps_unfiltered_campaign() -> None:
    cases = [
        bench.DemoCase(
            case_id=case_id,
            title=case_id,
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        )
        for case_id in (
            "reasoning_cross_file_triage_nanoagents",
            "workflow_hdf5_overview",
            "missing_hdf5_error",
        )
    ]

    selected, missing = bench._select_cases(cases, lane="all", case_ids=())

    assert missing == []
    assert selected == cases


def test_cancelled_case_passes_only_structured_cancelled_without_text() -> None:
    result = _result("claude_cancellation_surface")

    assert result.passed is True
    assert result.outcome == "cancelled"
    row = bench._case_row(result)
    assert row["stream_source"] == "batch"
    assert row["benchmark_lane"] == "claude_code"


def test_case_row_records_route_file_and_artifact_evidence(tmp_path) -> None:
    data_file = tmp_path / "sample.parquet"
    data_file.write_text("placeholder", encoding="utf-8")
    png_input = tmp_path / "cells.png"
    png_input.write_bytes(b"png-input")
    artifact = tmp_path / "chart.png"
    artifact.write_bytes(b"png")
    message = _message(
        text=f"Saved chart to {artifact}",
        tools=[
            {
                "name": "plot_summary",
                "args": {"filepath": str(data_file)},
                "result": {"artifact_path": str(artifact)},
            }
        ],
    )
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "analysis", "stage": "planner_dispatch"},
        {"agent_id": "visualization", "stage": "handoff"},
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="artifact_case",
            title="artifact",
            category="test",
            prompt=f"Create a plot from {data_file} and inspect {png_input}",
            why="why",
            expected="expected",
            session_group="test",
            expected_tools=("plot_summary",),
        ),
        session_id="sess_test",
        elapsed_s=2.0,
        message=message,
        provider={"provider": "claude_code", "model": "sonnet", "api_base": ""},
        semantic_events=[
            {
                "event_type": "turn.started",
                "trace_id": "trace_msg_user_1",
                "turn_id": "msg_user_1",
                "live_observed": True,
            },
            {
                "event_type": "tool.call.completed",
                "trace_id": "trace_msg_user_1",
                "turn_id": "msg_user_1",
                "live_observed": True,
            },
        ],
    )

    row = bench._case_row(result)

    assert row["data_files"] == [str(data_file), str(png_input)]
    assert row["artifact_evidence"] == [
        {"path": str(artifact), "exists": True, "size_bytes": 3}
    ]
    assert row["route_metrics"]["expert_depth"] == 2
    assert row["route_metrics"]["tool_call_count"] == 1
    assert row["route_graph"]["edges"][:2] == [
        {"from": "orchestrator", "to": "analysis", "kind": "route"},
        {"from": "analysis", "to": "visualization", "kind": "handoff"},
    ]
    assert row["semantic_trace"] == {
        "event_count": 2,
        "live_event_count": 2,
        "event_types": ["turn.started", "tool.call.completed"],
        "unique_event_types": ["tool.call.completed", "turn.started"],
        "trace_ids": ["trace_msg_user_1"],
        "turn_ids": ["msg_user_1"],
        "has_live_trace": True,
    }


def test_case_row_records_semantic_proof_declarations() -> None:
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="semantic_case",
            title="semantic",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            forbidden_route_sources=("guard",),
            semantic_proofs=("no_shortcuts", "root_delegation"),
        ),
        session_id="sess_semantic",
        elapsed_s=1.0,
        message=_message(route_source="dspy"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
    )

    row = bench._case_row(result)
    rehydrated = bench._result_from_case_row(row)

    assert row["semantic_proofs"] == ["no_shortcuts", "root_delegation"]
    assert row["observed_semantic_proofs"] == ["no_shortcuts", "root_delegation"]
    assert rehydrated.case.semantic_proofs == ("no_shortcuts", "root_delegation")


def test_format_live_event_line_renders_compact_semantic_event() -> None:
    line = bench._format_live_event_line(
        {
            "type": "semantic.event",
            "occurred_at": "2026-06-03T00:00:00+00:00",
            "payload": {
                "event_type": "llm.request.started",
                "status": "running",
                "summary": "LLM request started for data.",
                "actor": {"agent_id": "data"},
            },
        }
    )

    assert line == (
        "semantic llm.request.started | agent=data | status=running | "
        "LLM request started for data."
    )


def test_format_live_event_line_ignores_heartbeat_noise() -> None:
    assert bench._format_live_event_line({"type": "server.connected", "payload": {}}) == ""
    assert bench._format_live_event_line({"type": "server.heartbeat", "payload": {}}) == ""


def test_live_event_watch_starts_before_turn_and_stops_after(monkeypatch) -> None:
    calls: list[str] = []

    class FakeWatch:
        def __init__(self, *_args, enabled: bool, **_kwargs) -> None:
            self.enabled = enabled

        def __enter__(self):
            assert self.enabled is True
            calls.append("watch_enter")
            return self

        def __exit__(self, *_exc: object) -> None:
            calls.append("watch_exit")

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeLog:
        def write(self, _text: str) -> None:
            calls.append("log_write")

        def flush(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, path: str) -> FakeResponse:
            if path == "/v1/health":
                return FakeResponse({})
            raise AssertionError(path)

    case = bench.DemoCase(
        case_id="watch_case",
        title="watch",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="watch",
    )
    message = _message(text="ok")
    message["id"] = "msg_assistant"

    monkeypatch.setattr(bench.httpx, "Client", FakeClient)
    monkeypatch.setattr(bench, "create_benchmark_data", lambda _data_dir: {})
    monkeypatch.setattr(bench, "_make_cases", lambda _manifest: [case])
    monkeypatch.setattr(
        bench,
        "_create_sessions",
        lambda _http, _cases, workspace_id="": {bench._session_key(case): "sid"},
    )
    monkeypatch.setattr(bench, "_children", lambda _http, _session_id: [])
    monkeypatch.setattr(bench, "_provider", lambda _http: {})
    monkeypatch.setattr(bench, "_session_agent_blueprint", lambda _http, _session_id: {})
    monkeypatch.setattr(bench, "_chronological_session_messages", lambda _http, _session_id: [])
    monkeypatch.setattr(
        bench,
        "_semantic_events_for_completed_message",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(bench, "_LiveEventWatch", FakeWatch)

    def fake_post_turn(*_args, **_kwargs):
        calls.append("post_turn")
        return message

    monkeypatch.setattr(bench, "_post_turn", fake_post_turn)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: FakeLog())
    monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: None)

    code = bench.run_benchmark(
        "http://example.test",
        Path("/tmp/data"),
        Path("/tmp/out.jsonl"),
        Path("/tmp/report.md"),
        lane="all",
        case_ids=("watch_case",),
        watch_events=True,
    )

    assert code == 0
    assert calls[:3] == ["watch_enter", "post_turn", "watch_exit"]


def test_failed_result_recovers_partial_route_evidence_from_semantic_events() -> None:
    message = _message(text="", error="provider_timeout", tools=[])
    message["parts"] = [{"type": "text", "text": ""}]
    message["metadata"]["expert_handoffs"] = []
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="failed_semantic",
            title="failed semantic",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="failed",
            expected_tools=("sac_fetch_earthscope_waveform",),
        ),
        session_id="sess_test",
        elapsed_s=300.0,
        message=message,
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": "codex://exec"},
        semantic_events=[
            {
                "event_type": "agent.invocation.started",
                "actor": {"agent_id": "main"},
                "status": "running",
            },
            {
                "event_type": "delegation.started",
                "actor": {"agent_id": "main"},
                "subject": {"agent_id": "data"},
                "payload": {
                    "agent_id": "data",
                    "parent_id": "main",
                    "stage": "delegate.started",
                    "delegation_lifecycle": "sync",
                },
            },
            {
                "event_type": "delegation.started",
                "actor": {"agent_id": "data"},
                "subject": {"agent_id": "ndp_catalog"},
                "payload": {
                    "agent_id": "ndp_catalog",
                    "parent_id": "data",
                    "stage": "delegate.started",
                    "delegation_lifecycle": "sync",
                },
            },
            {
                "event_type": "tool.call.completed",
                "actor": {"tool": "ndp_search_datasets"},
                "subject": {"call_id": "call_ndp"},
                "payload": {
                    "call_id": "call_ndp",
                    "tool": "ndp_search_datasets",
                    "ok": True,
                    "duration_ms": 12.0,
                },
            },
            {
                "event_type": "delegation.completed",
                "actor": {"agent_id": "ndp_catalog"},
                "subject": {"agent_id": "data"},
                "payload": {
                    "agent_id": "ndp_catalog",
                    "parent_id": "data",
                    "stage": "delegate.completed",
                    "delegation_lifecycle": "sync",
                },
            },
            {
                "event_type": "delegation.parent_resumed",
                "actor": {"agent_id": "data"},
                "subject": {"agent_id": "ndp_catalog"},
                "payload": {
                    "agent_id": "data",
                    "parent_id": "main",
                    "stage": "parent.resumed",
                    "resumed_from": "ndp_catalog",
                    "delegation_lifecycle": "sync",
                },
            },
            {
                "event_type": "delegation.completed",
                "actor": {"agent_id": "data"},
                "subject": {"agent_id": "main"},
                "payload": {
                    "agent_id": "data",
                    "parent_id": "main",
                    "stage": "delegate.completed",
                    "delegation_lifecycle": "sync",
                },
            },
            {
                "event_type": "delegation.parent_resumed",
                "actor": {"agent_id": "main"},
                "subject": {"agent_id": "data"},
                "payload": {
                    "agent_id": "main",
                    "stage": "parent.resumed",
                    "resumed_from": "data",
                    "delegation_lifecycle": "sync",
                },
            },
        ],
    )

    row = bench._case_row(result)

    assert result.passed is False
    assert row["selected_agent"] == "main"
    assert row["tool_names"] == ["ndp_search_datasets"]
    assert row["expert_handoffs"][0]["telemetry_source"] == "semantic_event"
    assert row["route_metrics"]["expert_depth"] == 3
    assert row["route_metrics"]["sync_handoff_count"] == 2
    assert bench._missing_sync_return_pairs(result) == []
    assert row["route_graph"]["edges"][:4] == [
        {"from": "main", "to": "data", "kind": "handoff"},
        {"from": "data", "to": "main", "kind": "return"},
        {"from": "data", "to": "ndp_catalog", "kind": "handoff"},
        {"from": "ndp_catalog", "to": "data", "kind": "return"},
    ]


def test_route_graph_summary_preserves_sync_delegation_returns() -> None:
    message = _message(route_source="dspy")
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "data", "stage": "planner_dispatch"},
        {
            "agent_id": "ndp_catalog",
            "parent_id": "data",
            "stage": "planner_dispatch_child",
        },
        {
            "agent_id": "ndp_catalog",
            "parent_id": "data",
            "stage": "delegate.completed",
            "metadata": {
                "delegation_lifecycle": "sync",
                "return_to": "data",
            },
        },
        {
            "agent_id": "data",
            "stage": "parent.resumed",
            "metadata": {
                "delegation_lifecycle": "sync",
                "resumed_from": "ndp_catalog",
            },
        },
        {"agent_id": "analysis", "stage": "planner_dispatch"},
        {
            "agent_id": "sac_format",
            "parent_id": "analysis",
            "stage": "delegate.completed",
            "return_to": "analysis",
        },
        {
            "agent_id": "analysis",
            "stage": "parent.resumed",
            "resumed_from": "sac_format",
        },
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="ndp",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_handoff_agents=("ndp_catalog", "sac_format"),
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    summary = bench._route_graph_summary(result)

    assert "orchestrator -> data" in summary
    assert "data -> ndp_catalog -> data" in summary
    assert "analysis -> sac_format -> analysis" in summary
    assert {"from": "analysis", "to": "sac_format", "kind": "handoff"} in result.route_graph[
        "edges"
    ]
    assert bench._missing_sync_return_pairs(result) == []


def test_data_file_paths_ignore_scientific_slash_terms(tmp_path) -> None:
    mzml = tmp_path / "proteomics_qc.mzML"
    prompt = f"Review {mzml}. Include m/z coverage and intensity/TIC evidence."

    paths = bench._data_file_paths(prompt, [])

    assert paths == [str(mzml)]


def test_expected_error_allows_structured_handoff_telemetry_only() -> None:
    result = _result("missing_hdf5_error")
    result.message["parts"].append(
        {
            "type": "text",
            "text": "\ndata | failure | direct_tool",
        }
    )

    assert result.passed is True
    assert result.outcome == "expected_error"

    result.message["parts"].append({"type": "text", "text": "The missing file looked valid."})

    assert result.passed is False


def test_ndp_waveform_case_requires_sac_and_png_path() -> None:
    message = _message(
        text="SAC statistics were computed and a .png path was mentioned, but no artifact exists.",
        tools=[{"name": "ndp_stage_resource"}, {"name": "sac_compute_trace_statistics"}],
    )
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "ndp_catalog"},
        {"agent_id": "sac_format"},
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="ndp waveform",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_agent="data",
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_terms=("SAC", ".png"),
            min_artifacts=1,
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    assert result.passed is False


def test_nested_expert_handoffs_count_for_case_expectations(tmp_path: Path) -> None:
    png = tmp_path / "waveform.png"
    png.write_bytes(b"png")
    message = _message(
        text=f"SAC statistics complete and PNG artifact saved at {png}",
        tools=[
            {"name": "ndp_stage_resource"},
            {"name": "sac_compute_trace_statistics"},
            {"name": "sac_plot_traces", "result": {"output_path": str(png)}},
        ],
    )
    message["parts"][0]["selected_agent"] = "main"
    message["metadata"]["expert_handoffs"] = [
        {
            "agent_id": "data",
            "children": [{"agent_id": "ndp_catalog"}],
            "output_summary": "NDP blocker returned.",
        },
        {
            "agent_id": "analysis",
            "children": [{"agent_id": "sac_format"}],
            "output_summary": "SAC statistics complete.",
        },
        {"agent_id": "visualization", "output_summary": f"PNG artifact: {png}"},
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="marketplace_seismic_waveform_review",
            title="marketplace seismic",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="seismic-waveform-review",
            expected_agent=("data", "analysis", "visualization", "main"),
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_tools=("sac_plot_traces",),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_terms=("SAC", ".png"),
            min_artifacts=1,
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
        agent_blueprint={"active_agent_blueprint_id": "seismic-waveform-review"},
    )

    assert result.handoff_agent_ids == ["data", "ndp_catalog", "analysis", "sac_format", "visualization"]
    assert result.passed is True


def test_route_metrics_count_sync_handoff_edges() -> None:
    message = _message(text="workflow complete")
    message["parts"][0]["selected_agent"] = "main"
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "main", "stage": "planner_dispatch"},
        {
            "agent_id": "data",
            "parent_id": "main",
            "stage": "planner_dispatch_child",
        },
        {
            "agent_id": "data",
            "parent_id": "main",
            "stage": "delegate.completed",
            "metadata": {"return_to": "main"},
        },
        {
            "agent_id": "main",
            "stage": "parent.resumed",
            "metadata": {"resumed_from": "data"},
        },
        {
            "agent_id": "ndp_catalog",
            "parent_id": "data",
            "stage": "planner_dispatch_child",
        },
        {
            "agent_id": "ndp_catalog",
            "parent_id": "data",
            "stage": "delegate.completed",
            "metadata": {"return_to": "data"},
        },
        {
            "agent_id": "data",
            "parent_id": "main",
            "stage": "parent.resumed",
            "metadata": {"resumed_from": "ndp_catalog"},
        },
        {
            "agent_id": "visualization",
            "parent_id": "main",
            "stage": "planner_dispatch_child",
        },
        {
            "agent_id": "visualization",
            "parent_id": "main",
            "stage": "delegate.completed",
            "metadata": {"return_to": "main"},
        },
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="sync_handoff_metrics",
            title="sync",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        ),
        session_id="sess_sync",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    assert result.route_metrics["sync_handoff_count"] == 3
    assert result.route_metrics["child_session_branch_count"] == 0
    assert result.route_metrics["branch_count"] == 3


def test_remote_png_urls_do_not_count_as_local_artifacts(tmp_path: Path) -> None:
    png = tmp_path / "local.png"
    png.write_bytes(b"png")
    message = _message(
        text=(
            "Remote reference https://example.org/generated.png and "
            f"local artifact {png}"
        ),
        tools=[],
    )

    assert bench._artifact_paths(message) == [str(png)]


def test_expected_terms_can_match_tool_and_handoff_evidence() -> None:
    message = _message(
        text="Reference has two contigs and expected variant consequences.",
        tools=[
            {
                "name": "genomics_inspect_fasta",
                "result": {"records": [{"id": "chrA"}, {"id": "plasmidB"}]},
            },
            {
                "name": "genomics_summarize_vcf",
                "result": {"effects": ["missense", "frameshift"]},
            },
        ],
    )
    message["parts"][0]["selected_agent"] = "genomics"
    message["metadata"]["expert_handoffs"] = [
        {
            "agent_id": "genomics",
            "output_summary": "FASTA and VCF tool evidence returned.",
        }
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="genomics_reference_variant_review",
            title="genomics",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_agent="genomics",
            expected_tools=("genomics_inspect_fasta", "genomics_summarize_vcf"),
            expected_terms=("chrA", "plasmidB", "missense", "frameshift"),
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    assert result.passed is True


def test_rehydrated_ndp_waveform_case_preserves_artifact_requirement() -> None:
    message = _message(
        text="SAC statistics were computed and a .png path was mentioned, but no artifact exists.",
        tools=[{"name": "ndp_stage_resource"}, {"name": "sac_compute_trace_statistics"}],
    )
    message["parts"][0]["selected_agent"] = "data"
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "ndp_catalog"},
        {"agent_id": "sac_format"},
    ]
    recorded = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="ndp waveform",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    rehydrated = bench._result_from_case_row(bench._case_row(recorded))

    assert rehydrated.case.min_artifacts == 1
    assert rehydrated.passed is False


def test_ndp_waveform_case_accepts_later_phase_selected_agent() -> None:
    cases = bench._canonical_cases_by_id()
    case = cases["ndp_seismic_waveform_to_plot"]

    assert case.expected_agent == ("data", "analysis", "visualization")


def test_rehydrated_genomics_case_preserves_tool_evidence_term_matching() -> None:
    message = _message(
        text="Reference and variant review completed.",
        tools=[
            {
                "name": "genomics_inspect_fasta",
                "result": {"records": [{"id": "chrA"}, {"id": "plasmidB"}]},
            },
            {
                "name": "genomics_summarize_vcf",
                "result": {"effects": ["missense", "frameshift"]},
            },
        ],
    )
    message["parts"][0]["selected_agent"] = "genomics"
    recorded = bench.DemoResult(
        case=bench.DemoCase(
            case_id="genomics_reference_variant_review",
            title="genomics",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    rehydrated = bench._result_from_case_row(bench._case_row(recorded))

    assert rehydrated.passed is True


def test_case_alternate_criteria_keep_strict_failures() -> None:
    message = _message(text="NDP catalog only", tools=[{"name": "ndp_search_datasets"}])
    message["metadata"]["expert_handoffs"] = [{"agent_id": "ndp_catalog"}]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="ndp waveform",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_agent="data",
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_terms=("SAC", ".png"),
            min_artifacts=1,
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
    )

    assert result.passed is False


def test_claude_provider_lane_audit_requires_key_evidence() -> None:
    results = [
        _result("workflow_hdf5_overview"),
        _result("workflow_parquet_profile"),
        _result("reasoning_cross_file_triage_nanoagents"),
        _result("missing_hdf5_error"),
        _result("claude_cancellation_surface"),
    ]

    audit = bench._provider_lane_audit(results, "claude_code")

    assert audit
    assert all(item["passed"] for item in audit)
    assert {item["criterion"] for item in audit} >= {
        "planner JSON/routing reliability case passes",
        "tool-call argument generation cases pass",
        "stream provenance captured",
        "cancellation surfaces as structured cancelled turn",
        "provider-specific failures stay visible",
    }


def test_render_report_includes_provider_lane_audit(tmp_path) -> None:
    results = [
        _result("workflow_hdf5_overview"),
        _result("workflow_parquet_profile"),
        _result("reasoning_cross_file_triage_nanoagents"),
        _result("missing_hdf5_error"),
        _result("claude_cancellation_surface"),
    ]
    results[0].semantic_events = [
        {
            "event_type": "turn.started",
            "trace_id": "trace_msg_user_1",
            "turn_id": "msg_user_1",
            "live_observed": True,
        },
        {
            "event_type": "llm.request.started",
            "trace_id": "trace_msg_user_1",
            "turn_id": "msg_user_1",
            "live_observed": True,
        },
    ]

    report = bench._render_report(results, tmp_path / "evidence.jsonl")

    assert "# CLIO Claude Code Real-Provider Benchmark Report" in report
    assert "Benchmark lane: `claude_code`" in report
    assert "## Evidence Summary" in report
    assert "## Provider Lane Audit" in report
    assert "## Extended Stress Coverage Audit" in report
    assert "Semantic trace events captured: 2 events across 1/5 cases (2 live-observed)" in report
    assert "Semantic event types: llm.request.started, turn.started" in report
    assert "Semantic trace: 2 events, 2 live, types=llm.request.started, turn.started" in report
    assert "documented benchmark standard" not in report


def test_forbidden_route_source_fails_real_orchestrator_case() -> None:
    case = bench.DemoCase(
        case_id="real_case",
        title="real case",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="test",
        forbidden_route_sources=("guard", "user_agent_keyword", "recovery"),
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_test",
        elapsed_s=1.0,
        message=_message(route_source="guard"),
        provider={"provider": "claude_code", "model": "sonnet", "api_base": ""},
    )

    assert result.route_source == "guard"
    assert result.passed is False
    row = bench._case_row(result)
    assert row["forbidden_route_sources"] == ["guard", "user_agent_keyword", "recovery"]


def test_case_row_includes_full_session_logs() -> None:
    case = bench.DemoCase(
        case_id="session_log_case",
        title="session log",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="test",
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_root",
        elapsed_s=1.0,
        message=_message(),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        child_sessions=[{"id": "sess_child", "title": "child"}],
        session_messages=[
            {"id": "msg_user", "role": "user", "parts": [{"type": "text", "text": "prompt"}]},
            {"id": "msg_asst", "role": "assistant", "parts": [{"type": "text", "text": "ok"}]},
        ],
        child_session_messages={
            "sess_child": [
                {"id": "msg_child_user", "role": "user"},
                {"id": "msg_child_asst", "role": "assistant"},
            ]
        },
    )

    row = bench._case_row(result)

    assert row["session_log"]["root_session_id"] == "sess_root"
    assert [message["id"] for message in row["session_log"]["root_messages"]] == [
        "msg_user",
        "msg_asst",
    ]
    assert row["session_log"]["child_sessions"] == [
        {
            "session_id": "sess_child",
            "session": {"id": "sess_child", "title": "child"},
            "messages": [
                {"id": "msg_child_user", "role": "user"},
                {"id": "msg_child_asst", "role": "assistant"},
            ],
        }
    ]


def test_chronological_session_messages_sorts_api_response() -> None:
    class Response:
        @staticmethod
        def json() -> dict[str, object]:
            return {
                "messages": [
                    {"id": "msg_asst", "created_at": "2026-05-29T00:00:02+00:00"},
                    {"id": "msg_user", "created_at": "2026-05-29T00:00:01+00:00"},
                ]
            }

    class Client:
        @staticmethod
        def get(path: str) -> Response:
            assert path == "/v1/sessions/sess/messages"
            return Response()

    messages = bench._chronological_session_messages(Client(), "sess")

    assert [message["id"] for message in messages] == ["msg_user", "msg_asst"]


def test_render_existing_jsonl_tolerates_missing_session_log() -> None:
    result = bench._result_from_case_row(
        {
            "case": "old_case",
            "title": "old case",
            "category": "test",
            "prompt": "prompt",
            "expected": "expected",
            "why": "why",
            "session_id": "sess_old",
            "elapsed_s": 1.0,
            "provider": {"provider": "codex", "model": "gpt-5.5"},
        }
    )

    assert result.session_messages == []
    assert result.child_session_messages == {}


def test_real_orchestrator_lane_audit_requires_no_shortcuts() -> None:
    good = bench.DemoResult(
        case=bench.DemoCase(
            case_id="reasoning_cross_file_triage_nanoagents",
            title="good",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            forbidden_route_sources=("guard",),
        ),
        session_id="sess_good",
        elapsed_s=1.0,
        message=_message(route_source="dspy"),
        provider={"provider": "claude_code", "model": "sonnet", "api_base": ""},
        benchmark_lane="real_orchestrator",
    )
    shortcut = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="shortcut",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            forbidden_route_sources=("guard",),
        ),
        session_id="sess_shortcut",
        elapsed_s=1.0,
        message=_message(route_source="guard"),
        provider={"provider": "claude_code", "model": "sonnet", "api_base": ""},
        benchmark_lane="real_orchestrator",
    )

    audit = bench._provider_lane_audit([good, shortcut], "real_orchestrator")

    shortcut_row = next(
        item for item in audit if item["criterion"] == "all selected cases avoid shortcut route sources"
    )
    assert shortcut_row["passed"] is False
    assert shortcut_row["observed"] == 1


def test_real_orchestrator_lane_audit_requires_artifact_verification() -> None:
    case = bench.DemoCase(
        case_id="visual_case",
        title="visual",
        category="test",
        prompt="make a png",
        why="why",
        expected="expected",
        session_group="test",
        expected_terms=(".png",),
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_visual",
        elapsed_s=1.0,
        message=_message(text="Saved chart to /tmp/clio-missing-artifact.png"),
        provider={"provider": "claude_code", "model": "sonnet", "api_base": ""},
        benchmark_lane="real_orchestrator",
    )

    audit = bench._provider_lane_audit([result], "real_orchestrator")

    artifact_row = next(
        item for item in audit if item["criterion"] == "artifact-producing cases verify artifacts on disk"
    )
    assert artifact_row["passed"] is False
    assert artifact_row["observed"] == 0


def test_real_orchestrator_ndp_blocker_audit_requires_sac_plot() -> None:
    message = _message(
        text="Staging note: bounded NDP attempts completed, but none could be staged.",
        tools=[{"name": "ndp_stage_resource"}],
    )
    message["metadata"]["expert_handoffs"] = [{"agent_id": "ndp_catalog"}]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="ndp waveform",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_agent="data",
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_terms=("SAC", ".png"),
            min_artifacts=1,
        ),
        session_id="sess_ndp",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="real_orchestrator",
    )

    audit = bench._provider_lane_audit([result], "real_orchestrator")

    artifact_row = next(
        item for item in audit if item["criterion"] == "artifact-producing cases verify artifacts on disk"
    )
    ndp_row = next(
        item
        for item in audit
        if item["criterion"] == "NDP waveform benchmark reaches verified SAC/PNG artifact"
    )
    full_chain_row = next(
        item for item in audit if item["criterion"] == "NDP full SAC/PNG chain verified"
    )
    assert artifact_row["required"] == 1
    assert artifact_row["observed"] == 0
    assert ndp_row["passed"] is False
    assert ndp_row["details"] == []
    assert full_chain_row["passed"] is False
    assert full_chain_row["observed"] == 0


def test_real_orchestrator_audit_requires_sync_parent_resume() -> None:
    message = _message(text="SAC plot saved at /tmp/plot.png", route_source="dspy")
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "data", "stage": "planner_dispatch"},
        {
            "agent_id": "ndp_catalog",
            "parent_id": "data",
            "stage": "planner_dispatch_child",
        },
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="ndp waveform",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_handoff_agents=("ndp_catalog",),
        ),
        session_id="sess_ndp",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="real_orchestrator",
    )

    audit = bench._provider_lane_audit([result], "real_orchestrator")

    sync_row = next(
        item
        for item in audit
        if item["criterion"] == "nested expert handoffs include sync return/resume provenance"
    )
    assert sync_row["passed"] is False
    assert sync_row["details"] == ["ndp_seismic_waveform_to_plot: missing=['data->ndp_catalog']"]


def test_semantic_regression_audit_reports_missing_proof_evidence() -> None:
    hierarchy = bench.DemoResult(
        case=bench.DemoCase(
            case_id="hierarchy",
            title="hierarchy",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            forbidden_route_sources=("guard",),
            semantic_proofs=("no_shortcuts", "root_delegation", "nested_tier3"),
        ),
        session_id="sess_hierarchy",
        elapsed_s=1.0,
        message=_message(route_source="dspy"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        child_sessions=[{"id": "child_1"}],
        benchmark_lane="semantic_regression",
    )
    memory_without_policy_evidence = bench.DemoResult(
        case=bench.DemoCase(
            case_id="memory",
            title="memory",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            semantic_proofs=("workspace_memory_scope",),
        ),
        session_id="sess_memory",
        elapsed_s=1.0,
        message=_message(text="continued from prior session context"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
    )

    audit = bench._provider_lane_audit(
        [hierarchy, memory_without_policy_evidence],
        "semantic_regression",
    )

    declared_row = next(
        item
        for item in audit
        if item["criterion"] == "semantic-regression lane declares required proof classes"
    )
    observed_row = next(
        item
        for item in audit
        if item["criterion"] == "semantic-regression passing evidence covers required proof classes"
    )
    case_row = next(
        item for item in audit if item["criterion"] == "each declared case proof is observed in session evidence"
    )
    assert declared_row["passed"] is False
    assert "command_mcp_skill_scope" in "\n".join(declared_row["details"])
    assert observed_row["passed"] is False
    assert "workspace_memory_scope" in "\n".join(observed_row["details"])
    assert case_row["passed"] is False
    assert case_row["details"] == [
        "memory: workspace_memory_scope declared but not observed"
    ]


def test_command_mcp_skill_scope_requires_structured_capability_evidence() -> None:
    case = bench.DemoCase(
        case_id="mcp_scope",
        title="mcp scope",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="semantic",
        semantic_proofs=("command_mcp_skill_scope",),
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_mcp",
        elapsed_s=1.0,
        message=_message(text="calculator_add is disabled until explicit trust"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
        agent_blueprint={
            "active_agent_blueprint_id": "mcp-calculator-smoke",
            "mcp_descriptors": [
                {
                    "id": "calculator",
                    "enabled": False,
                    "tools": ["calculator_add"],
                    "trust": {"policy": "explicit"},
                }
            ],
        },
    )

    assert bench._case_observed_semantic_proofs(result) == ("command_mcp_skill_scope",)


def test_command_mcp_skill_scope_ignores_unstructured_model_words() -> None:
    case = bench.DemoCase(
        case_id="mcp_scope",
        title="mcp scope",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="semantic",
        semantic_proofs=("command_mcp_skill_scope",),
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_mcp",
        elapsed_s=1.0,
        message=_message(text="This mentions command, mcp, skill, disabled, and trust."),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
    )

    assert bench._case_observed_semantic_proofs(result) == ()


def test_render_existing_jsonl_can_gate_missing_semantic_evidence(tmp_path: Path) -> None:
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="memory_scope",
            title="memory scope",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="semantic",
            semantic_proofs=("workspace_memory_scope",),
        ),
        session_id="sess_memory",
        elapsed_s=1.0,
        message=_message(text="continued from prior session context", route_source="dspy"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
    )
    evidence = tmp_path / "semantic.jsonl"
    evidence.write_text(
        bench.json.dumps(bench._case_row(result), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "semantic.md"

    exit_code = bench.render_report_from_jsonl(
        evidence,
        report,
        lane="semantic_regression",
        require_lane_criteria=True,
    )

    assert exit_code == 1
    report_text = report.read_text(encoding="utf-8")
    assert "semantic-regression passing evidence covers required proof classes" in report_text
    assert "workspace_memory_scope" in report_text


def test_render_existing_jsonl_without_gate_keeps_report_rendering_permissive(tmp_path: Path) -> None:
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="memory_scope",
            title="memory scope",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="semantic",
            semantic_proofs=("workspace_memory_scope",),
        ),
        session_id="sess_memory",
        elapsed_s=1.0,
        message=_message(text="continued from prior session context", route_source="dspy"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
    )
    evidence = tmp_path / "semantic.jsonl"
    evidence.write_text(
        bench.json.dumps(bench._case_row(result), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "semantic.md"

    exit_code = bench.render_report_from_jsonl(evidence, report, lane="semantic_regression")

    assert exit_code == 0
    assert report.read_text(encoding="utf-8")


def test_marketplace_audit_requires_root_sync_delegation() -> None:
    message = _message(
        text="reference review complete",
        route_source="user_agent",
        tools=[{"name": "genomics_inspect_fasta"}],
    )
    message["parts"][0]["selected_agent"] = "reference"
    message["metadata"]["expert_handoffs"] = [
        {
            "agent_id": "reference",
            "parent_id": "main",
            "stage": "planner_dispatch_child",
        }
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="marketplace_genomics_reference_review",
            title="marketplace genomics",
            category="marketplace-test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="marketplace",
            agent_blueprint_id="genomics-review",
            expected_agent="main",
            expected_tools=("genomics_inspect_fasta",),
            expected_handoff_agents=("reference",),
        ),
        session_id="sess_marketplace",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="marketplace_agents",
        agent_blueprint={"active_agent_blueprint_id": "genomics-review"},
    )

    audit = bench._provider_lane_audit([result], "marketplace_agents")

    root_row = next(
        item
        for item in audit
        if item["criterion"] == "marketplace hierarchy cases prove root sync delegation"
    )
    assert root_row["passed"] is False
    assert root_row["observed"] == 0
    assert root_row["details"] == [
        "marketplace_genomics_reference_review: selected=reference "
        "handoffs=['reference'] missing_returns=['main->reference']"
    ]


def test_case_minimum_hierarchy_thresholds_affect_pass_status() -> None:
    message = _message(
        text="SAC plot saved at /tmp/plot.png",
        tools=[{"name": "sac_fetch_earthscope_waveform"}],
    )
    message["parts"][0]["selected_agent"] = "main"
    message["metadata"]["expert_handoffs"] = [
        {"agent_id": "main", "stage": "planner_dispatch"},
        {
            "agent_id": "ndp_catalog",
            "parent_id": "main",
            "stage": "planner_dispatch_child",
        },
        {
            "agent_id": "ndp_catalog",
            "parent_id": "main",
            "stage": "delegate.completed",
            "metadata": {"delegation_lifecycle": "sync", "return_to": "main"},
        },
        {
            "agent_id": "main",
            "stage": "parent.resumed",
            "metadata": {"delegation_lifecycle": "sync", "resumed_from": "ndp_catalog"},
        },
    ]
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="hierarchy_threshold",
            title="threshold",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_agent="main",
            expected_tools=("sac_fetch_earthscope_waveform",),
            expected_handoff_agents=("ndp_catalog",),
            min_expert_depth=3,
            min_branch_count=2,
        ),
        session_id="sess_threshold",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="marketplace_agents",
    )

    assert result.route_metrics["expert_depth"] == 2
    assert result.route_metrics["branch_count"] == 1
    assert result.passed is False
    row = bench._case_row(result)
    assert row["min_expert_depth"] == 3
    assert row["min_branch_count"] == 2


def test_marketplace_audit_distinguishes_complex_hierarchy_from_smoke() -> None:
    def marketplace_result(
        case_id: str,
        blueprint_id: str,
        handoffs: list[dict[str, object]],
        *,
        tools: list[str],
    ) -> bench.DemoResult:
        message = _message(
            text=f"{case_id} complete",
            route_source="dspy",
            tools=[{"name": tool} for tool in tools],
        )
        message["parts"][0]["selected_agent"] = "main"
        message["metadata"]["expert_handoffs"] = handoffs
        return bench.DemoResult(
            case=bench.DemoCase(
                case_id=case_id,
                title=case_id,
                category="marketplace-test",
                prompt="prompt",
                why="why",
                expected="expected",
                session_group="marketplace",
                agent_blueprint_id=blueprint_id,
                expected_agent="main",
                expected_tools=(tools[0],),
                expected_handoff_agents=tuple(
                    str(row["agent_id"]) for row in handoffs if row.get("parent_id") == "main"
                ),
            ),
            session_id=f"sess_{case_id}",
            elapsed_s=1.0,
            message=message,
            provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
            benchmark_lane="marketplace_agents",
            agent_blueprint={"active_agent_blueprint_id": blueprint_id},
        )

    def sync_rows(parent: str, child: str) -> list[dict[str, object]]:
        return [
            {"agent_id": child, "parent_id": parent, "stage": "planner_dispatch_child"},
            {
                "agent_id": child,
                "parent_id": parent,
                "stage": "delegate.completed",
                "metadata": {"delegation_lifecycle": "sync", "return_to": parent},
            },
            {
                "agent_id": parent,
                "stage": "parent.resumed",
                "metadata": {"delegation_lifecycle": "sync", "resumed_from": child},
            },
        ]

    shallow = marketplace_result(
        "marketplace_shallow",
        "shallow-pack",
        [{"agent_id": "main", "stage": "planner_dispatch"}, *sync_rows("main", "reference")],
        tools=["genomics_inspect_fasta"],
    )
    complex_cases = [
        marketplace_result(
            f"marketplace_complex_{index}",
            f"complex-pack-{index}",
            [
                {"agent_id": "main", "stage": "planner_dispatch"},
                *sync_rows("main", "data"),
                *sync_rows("data", "catalog"),
            ],
            tools=["ndp_search_datasets"],
        )
        for index in range(3)
    ]

    audit = bench._provider_lane_audit([shallow, *complex_cases], "marketplace_agents")

    complex_row = next(
        item
        for item in audit
        if item["criterion"] == "at least three marketplace cases prove complex hierarchy depth"
    )
    smoke_row = next(
        item
        for item in audit
        if item["criterion"] == "marketplace shallow cases are reported as smoke coverage"
    )
    assert complex_row["passed"] is True
    assert complex_row["observed"] == 3
    assert smoke_row["passed"] is True
    assert smoke_row["observed"] == 1
    assert "marketplace_shallow" in smoke_row["details"][0]


def test_render_report_from_multiple_jsonls_combines_marketplace_evidence(tmp_path: Path) -> None:
    blueprints = [
        ("marketplace_genomics_reference_review", "genomics-review", "reference", "genomics_inspect_fasta"),
        (
            "marketplace_materials_crystal_review",
            "materials-crystal-review",
            "crystal_structure",
            "materials_inspect_cif",
        ),
        (
            "marketplace_geospatial_field_review",
            "geospatial-field-review",
            "spatial_features",
            "geospatial_inspect_geojson",
        ),
        (
            "marketplace_proteomics_mzml_review",
            "proteomics-mzml-review",
            "mass_spec",
            "mass_spec_inspect_mzml",
        ),
        ("marketplace_seismic_waveform_review", "seismic-waveform-review", "data", "sac_plot_traces"),
    ]
    rows: list[dict[str, object]] = []
    for case_id, blueprint_id, child_agent, tool_name in blueprints:
        message = _message(
            text=f"{blueprint_id} completed",
            route_source="user_agent",
            tools=[{"name": tool_name}],
        )
        message["parts"][0]["selected_agent"] = "main"
        message["metadata"]["expert_handoffs"] = [
            {"agent_id": "main", "stage": "planner_dispatch"},
            {
                "agent_id": child_agent,
                "parent_id": "main",
                "stage": "planner_dispatch_child",
            },
            {
                "agent_id": child_agent,
                "parent_id": "main",
                "stage": "delegate.completed",
                "metadata": {"delegation_lifecycle": "sync", "return_to": "main"},
            },
            {
                "agent_id": "main",
                "stage": "parent.resumed",
                "metadata": {"delegation_lifecycle": "sync", "resumed_from": child_agent},
            },
        ]
        result = bench.DemoResult(
            case=bench.DemoCase(
                case_id=case_id,
                title=case_id,
                category="marketplace-test",
                prompt="prompt",
                why="why",
                expected="expected",
                session_group="marketplace",
                agent_blueprint_id=blueprint_id,
                expected_agent="main",
                expected_tools=(tool_name,),
                expected_handoff_agents=(child_agent,),
            ),
            session_id=f"sess_{case_id}",
            elapsed_s=1.0,
            message=message,
            provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
            benchmark_lane="marketplace_agents",
            agent_blueprint={"active_agent_blueprint_id": blueprint_id},
        )
        rows.append(bench._case_row(result))

    first = tmp_path / "marketplace-a.jsonl"
    second = tmp_path / "marketplace-b.jsonl"
    first.write_text(
        "".join(bench.json.dumps(row, sort_keys=True) + "\n" for row in rows[:3]),
        encoding="utf-8",
    )
    second.write_text(
        "".join(bench.json.dumps(row, sort_keys=True) + "\n" for row in rows[3:]),
        encoding="utf-8",
    )
    combined = tmp_path / "combined.jsonl"
    report = tmp_path / "combined.md"

    bench.render_report_from_jsonls([first, second], combined, report)

    assert len(combined.read_text(encoding="utf-8").splitlines()) == 5
    report_text = report.read_text(encoding="utf-8")
    assert "at least five distinct marketplace Agent Blueprints | 5 | 5 | pass" in report_text
    assert "seismic-waveform-review" in report_text
    assert "genomics-review" in report_text
