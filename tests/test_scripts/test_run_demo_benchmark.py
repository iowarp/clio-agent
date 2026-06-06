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
    expert_handoffs: list[dict[str, object]] | None = None,
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
            "expert_handoffs": expert_handoffs or [],
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
            "marketplace_mcp_calculator_scope",
            "marketplace_mcp_calculator_enabled_call",
            "marketplace_packaged_hook_blocked_turn",
            "workspace_memory_scope_policy",
            "provider_swap_memory_followup",
        )
    ]

    selected, missing = bench._select_cases(cases, lane="semantic_regression", case_ids=())

    assert missing == []
    assert [case.case_id for case in selected] == list(
        bench._BENCHMARK_LANES["semantic_regression"]
    )


def test_select_marketplace_agents_lane_cases() -> None:
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
        for case_id in bench._BENCHMARK_LANES["marketplace_agents"]
    ]

    selected, missing = bench._select_cases(cases, lane="marketplace_agents", case_ids=())

    assert missing == []
    assert [case.case_id for case in selected] == list(
        bench._BENCHMARK_LANES["marketplace_agents"]
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
        "invalid_tool_selection_count": 0,
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
    assert result.blocking_error is not None
    assert result.blocking_error["error"] == "provider_timeout"
    assert result.failure_reasons() == []
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


def test_semantic_completed_delegation_rehydrates_parent_resume() -> None:
    message = _message(route_source="agent_blueprint")
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="marketplace_hpc_io_regression",
            title="hpc",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="hpc-io-regression",
            expected_handoff_agents=("baseline_ingest",),
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={},
        agent_blueprint={"active_agent_blueprint_id": "hpc-io-regression"},
        semantic_events=[
            {
                "event_type": "blueprint.delegation.started",
                "actor": {"agent_id": "trace_ingest"},
                "subject": {"agent_id": "baseline_ingest"},
                "payload": {},
            },
            {
                "event_type": "blueprint.delegation.completed",
                "actor": {"agent_id": "baseline_ingest"},
                "subject": {"agent_id": "trace_ingest"},
                "payload": {},
            },
        ],
    )

    handoffs = result.expert_handoffs

    assert {
        "agent_id": "trace_ingest",
        "stage": "parent.resumed",
        "resumed_from": "baseline_ingest",
        "inferred_from": "delegation.completed",
    }.items() <= handoffs[-1].items()
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


def test_case_row_observed_excerpt_prefers_completed_synthesis_handoff() -> None:
    message = _message(
        text="NEXT_EXPERT: analysis NEXT_ACTION: continue orchestration",
        expert_handoffs=[
            {
                "agent_id": "analysis",
                "status": "completed",
                "output_summary": "analysis evidence",
            },
            {
                "agent_id": "synthesis",
                "status": "completed",
                "output_summary": "final scientific brief",
            },
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="marketplace_earthscope_gnss_region_coordinate_mutation",
            title="earthscope",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "sophia", "model": "gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.observed_excerpt_text == "final scientific brief"
    assert bench._case_row(result)["answer_excerpt"] == "final scientific brief"


def test_case_row_prefers_visible_answer_over_internal_handoff_state() -> None:
    message = _message(
        text="final clean EarthScope brief",
        expert_handoffs=[
            {
                "agent_id": "synthesis",
                "status": "completed",
                "output_summary": (
                    "final clean EarthScope brief\n\n"
                    "CLIO typed workflow state:\n"
                    '{"workflow_state":{"report":{"status":"ready"}}}'
                ),
            },
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="marketplace_earthscope_gnss_region_depth_topology",
            title="earthscope",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
        ),
        session_id="sess_test",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "sophia", "model": "gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.observed_excerpt_text == "final clean EarthScope brief"
    assert "CLIO typed workflow state" not in bench._case_row(result)["answer_excerpt"]


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


def test_relative_json_and_geojson_paths_count_as_durable_artifacts(tmp_path: Path) -> None:
    message = _message(
        text=(
            "Persisted compact evidence to .clio-agent-artifacts/ndp/current_wildfires_ca.json "
            "and map features to outputs/hazards.geojson."
        ),
        tools=[
            {
                "name": "ndp_query_arcgis_features",
                "args": {"output_path": "run/evidence/from_args.geojson"},
                "result": {
                    "output_path": str(tmp_path / "feature_evidence.json"),
                    "source_url": "https://example.org/FeatureServer",
                },
            }
        ],
    )

    assert bench._artifact_paths(message) == [
        "run/evidence/from_args.geojson",
        str(tmp_path / "feature_evidence.json"),
        ".clio-agent-artifacts/ndp/current_wildfires_ca.json",
        "outputs/hazards.geojson",
    ]


def test_compacted_partial_workspace_paths_do_not_count_as_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "WWMT.CI.LY_.40.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("time,east,north,up\n", encoding="utf-8")
    message = _message(
        text=(
            f"Valid staged path: `{artifact}`\n"
            "[tail]\n"
            "/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv\n"
            "io-agent/.clio/artifacts/ndp-staging/WWMT.CI.LY_.40.csv\n"
        )
    )

    assert bench._artifact_paths(message) == [str(artifact)]


def test_download_url_path_fragments_do_not_count_as_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "earthscope.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("station,lat,lon\n", encoding="utf-8")
    message = _message(
        text=(
            f"Staged station metadata at {artifact}.\n"
            "Source URL: https://nationaldataplatform.org/catalog/dataset/"
            "811f0bcc-99e5-455c-bcf6-7c63c2634f41/resource/"
            "a420cc30-2262-423a-8c63-3ad8d91f2a8f/download/"
            "earthscope_converted_data.csv\n"
            "Short download fragment: /download/earthscope_converted_data.csv\n"
            "UUID download fragment: f2a8f/download/earthscope_converted_data.csv\n"
            "Compacted fragment: /resource/a420cc30-2262-423a-8c63-3ad8d91f2a8f/"
            "download/earthscope_converted_data.csv\n"
            "Relative fragment: resource/a420cc30-2262-423a-8c63-3ad8d91f2a8f/"
            "download/earthscope_converted_data.csv\n"
            "Tail fragment: 6-7c63c2634f41/resource/"
            "a420cc30-2262-423a-8c63-3ad8d91f2a8f/download/"
            "earthscope_converted_data.csv\n"
            "Remote path fragment: dec2024/raw_csv/MTA1.CI.LY_.30.csv\n"
            "Compacted local fragment: clio/artifacts/ndp-staging/MTA1_time_series.png\n"
        )
    )

    assert bench._artifact_paths(message) == [str(artifact)]


def test_earthscope_station_metadata_csv_counts_as_input_not_artifact(tmp_path: Path) -> None:
    metadata = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "earthscope_converted_data.csv"
    station_csv = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "MTA1.CI.LY_.30.csv"
    png = tmp_path / ".clio" / "artifacts" / "plots" / "MTA1_CI_LY_30_timeseries.png"
    metadata.parent.mkdir(parents=True)
    png.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text("Site,Latitude,Longitude\nUCSF,37.763,-122.458\n", encoding="utf-8")
    station_csv.write_text("time,east,north,up\n", encoding="utf-8")
    png.write_bytes(b"png")
    message = _message(
        text=(
            f"Staged metadata at {metadata}. "
            f"Staged station CSV at {station_csv}. "
            f"Generated plot at {png}."
        ),
        tools=[
            {
                "name": "ndp_stage_resource",
                "args": {"output_dir": str(metadata.parent)},
                "result": {"path": str(metadata), "status": "staged"},
            },
            {
                "name": "ndp_filter_earthscope_station_catalog",
                "args": {"filepath": str(metadata), "latitude": 37.77, "longitude": -122.42},
                "result": {"path": str(metadata), "within_radius_count": 67},
            },
            {
                "name": "ndp_stage_resource",
                "args": {"output_dir": str(station_csv.parent)},
                "result": {"path": str(station_csv), "status": "staged"},
            },
            {
                "name": "ndp_plot_csv_timeseries",
                "args": {"filepath": str(station_csv), "output_path": str(png)},
                "result": {"path": str(png), "status": "plotted"},
            },
        ],
    )

    assert bench._artifact_paths(message) == [str(station_csv), str(png)]
    assert bench._data_file_paths("", message["metadata"]["tools_called"]) == [
        str(metadata),
        str(station_csv),
    ]


def test_missing_absolute_tool_output_arg_does_not_count_as_artifact(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "station.csv"
    png_path = tmp_path / "station.png"
    missing_png = tmp_path / "missing" / "station.png"
    csv_path.write_text("time,east,north,up\n", encoding="utf-8")
    png_path.write_bytes(b"png")
    message = _message(
        text=f"First tried {missing_png}; generated plot at {png_path}",
        tools=[
            {
                "name": "ndp_plot_csv_timeseries",
                "args": {
                    "filepath": str(csv_path),
                    "output_path": str(missing_png),
                },
            },
            {
                "name": "ndp_plot_csv_timeseries",
                "args": {
                    "filepath": str(csv_path),
                    "output_path": str(png_path),
                },
            },
        ],
    )

    tools = message["metadata"]["tools_called"]

    assert bench._artifact_paths(message) == [str(csv_path), str(png_path)]
    assert bench._data_file_paths("", tools) == [str(csv_path)]


def test_coordinate_earthscope_case_accepts_typed_blocker_without_analysis(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "earthscope_converted_data.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("station,lat,lon\nMTA1,34.0,-118.2\n", encoding="utf-8")
    workflow_state = {
        "workflow_state": {
            "catalog": {"status": "metadata_found"},
            "acquisition": {
                "status": "metadata_only",
                "analysis_ready": False,
                "metadata_path": str(artifact),
                "blocker": "staged resource is station metadata, not a GNSS time-series CSV",
            },
        }
    }
    case = bench._canonical_cases_by_id()[
        "marketplace_earthscope_gnss_region_coordinate_mutation"
    ]
    message = _message(
        text=(
            "EarthScope GNSS acquisition blocker: station metadata was staged, "
            "but no analysis-ready GNSS time-series CSV was available.\n"
            f"{artifact}\n"
            f"{workflow_state}"
        ),
        tools=[
            {"name": "ndp_search_datasets", "result": {"datasets": []}},
            {"name": "ndp_stage_resource", "result": {"local_path": str(artifact)}},
        ],
        expert_handoffs=[
            {
                "agent_id": "geospatial",
                "parent_id": "main",
                "stage": "delegate.completed",
                "output_summary": '{"workflow_state":{"geospatial":{"status":"resolved"}}}',
            },
            {
                "agent_id": "main",
                "stage": "parent.resumed",
                "resumed_from": "geospatial",
            },
            {
                "agent_id": "data",
                "parent_id": "main",
                "stage": "delegate.completed",
                "output_summary": str(workflow_state),
            },
            {
                "agent_id": "ndp_dataset_discovery",
                "parent_id": "data",
                "stage": "delegate.completed",
                "output_summary": '{"workflow_state":{"catalog":{"status":"metadata_found"}}}',
            },
            {
                "agent_id": "earthscope_station_catalog",
                "parent_id": "data",
                "stage": "delegate.completed",
                "output_summary": '{"workflow_state":{"station_catalog":{"status":"ranked_metadata_only"}}}',
            },
            {
                "agent_id": "ndp_resource_resolver",
                "parent_id": "data",
                "stage": "delegate.completed",
                "output_summary": str(workflow_state),
            },
            {
                "agent_id": "synthesis",
                "parent_id": "main",
                "stage": "delegate.completed",
                "output_summary": "Final bounded blocker summary.",
            },
        ],
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_coordinate_blocker",
        elapsed_s=1.0,
        message=message,
        provider={},
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region"},
    )

    assert "analysis" not in result.handoff_agent_ids
    assert result.passed


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


def test_expected_terms_match_normalized_scientific_unicode() -> None:
    message = _message(
        text=(
            "Effects include Stop‑gained; material formula SrTiO₃ has space group "
            "P\u202fm\u202f-3\u202fm. HPC evidence reports write time and independent writes."
        ),
        tools=[],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="normalized_scientific_terms",
            title="normalized",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_terms=(
                "stop-gained",
                "SrTiO3",
                "P m -3 m",
                "write_time",
                "independent_writes",
            ),
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
        {
            "event_type": "tool.selection.invalid",
            "trace_id": "trace_msg_user_1",
            "turn_id": "msg_user_1",
            "live_observed": True,
            "payload": {
                "agent_id": "variant_impact",
                "requested_tool": "shell_bash",
                "allowed_tools": ["genomics_summarize_vcf"],
                "tool_executed": False,
            },
        },
    ]

    report = bench._render_report(results, tmp_path / "evidence.jsonl")

    assert "# CLIO Claude Code Real-Provider Benchmark Report" in report
    assert "Benchmark lane: `claude_code`" in report
    assert "## Evidence Summary" in report
    assert "## Provider Lane Audit" in report
    assert "## Extended Stress Coverage Audit" in report
    assert "Semantic trace events captured: 3 events across 1/5 cases (3 live-observed)" in report
    assert "Semantic event types: llm.request.started, tool.selection.invalid, turn.started" in report
    assert (
        "Invalid tool selections blocked: 1 "
        "(workflow_hdf5_overview:variant_impact->shell_bash)"
    ) in report
    assert (
        "Semantic trace: 3 events, 3 live, "
        "types=llm.request.started, tool.selection.invalid, turn.started"
    ) in report
    assert "documented benchmark standard" not in report


def test_render_report_surfaces_tool_result_evidence_gaps(tmp_path: Path) -> None:
    case = bench.DemoCase(
        case_id="earthscope_trace_review",
        title="EarthScope trace review",
        category="marketplace",
        prompt="Find EarthScope GNSS data for a region.",
        why="why",
        expected="expected",
        session_group="earthscope",
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_trace",
        elapsed_s=1.0,
        message=_message(
            text="Staged and analyzed data.",
            tools=[
                {
                    "name": "ndp_stage_resource",
                    "args": {"dataset_identifier": "dataset-1"},
                    "ok": True,
                    "telemetry_source": "live_observer",
                },
                {
                    "name": "ndp_profile_csv_resource",
                    "args": {"filepath": "station.csv"},
                    "result": {"columns": ["time", "east", "north", "up"]},
                    "ok": True,
                    "telemetry_source": "agent_trajectory",
                },
                {
                    "name": "ndp_search_datasets",
                    "args": {"search_terms": ["EarthScope"]},
                    "ok": False,
                    "error": "TimeoutError",
                    "telemetry_source": "live_observer",
                },
            ],
        ),
        provider={"provider": "argonne", "model": "gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.tool_result_evidence == {
        "tool_rows": 3,
        "successful_rows": 2,
        "failed_rows": 1,
        "resultful_rows": 1,
        "review_gap_tools": ["ndp_stage_resource"],
        "failed_tools": ["ndp_search_datasets"],
    }

    report = bench._render_report([result], tmp_path / "evidence.jsonl")

    assert "Reviewable scientific tool rows: 3 (2 successful, 1 failed)" in report
    assert "Successful tool rows with result evidence: 1/2" in report
    assert "## Tool Result Evidence Review" in report
    assert "`earthscope_trace_review`: `ndp_stage_resource`" in report
    assert "Tool result evidence: 1/2 successful rows carry result evidence; 1 failed rows" in report
    assert "failed tools: ndp_search_datasets" in report


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


def test_real_orchestrator_audit_no_longer_requires_sac_plot() -> None:
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
    assert artifact_row["required"] == 1
    assert artifact_row["observed"] == 0
    assert not any(
        "SAC/PNG" in str(item["criterion"]) or "waveform benchmark" in str(item["criterion"])
        for item in audit
    )


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
    assert "enabled_mcp_execution" in "\n".join(declared_row["details"])
    assert "packaged_hook_invocation" in "\n".join(declared_row["details"])
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


def test_enabled_mcp_execution_requires_enable_and_call_actions() -> None:
    case = bench.DemoCase(
        case_id="mcp_enabled",
        title="mcp enabled",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="semantic",
        semantic_proofs=("enabled_mcp_execution",),
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_mcp",
        elapsed_s=1.0,
        message=_message(text="calculator_add ready"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
        actions=[
            {
                "type": "agent_blueprint_mcp_enable",
                "ok": True,
                "ready_tools": ["calculator_add"],
                "trust": {"trusted": True, "source": "request"},
            },
            {
                "type": "mcp_tool_call",
                "ok": True,
                "tool": "calculator_add",
                "result": {"content": [{"type": "text", "text": '{"sum":7.0}'}]},
            },
        ],
    )

    assert bench._case_observed_semantic_proofs(result) == ("enabled_mcp_execution",)


def test_packaged_hook_invocation_requires_enable_and_trace_provenance() -> None:
    case = bench.DemoCase(
        case_id="hook_smoke",
        title="hook smoke",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="semantic",
        agent_blueprint_id="hook-smoke",
        semantic_proofs=("packaged_hook_invocation",),
    )
    hook_event = {
        "event_type": "hook.pre_message.blocked",
        "actor": {"hook": "pre_message"},
        "payload": {
            "handlers": [
                {
                    "source": "agent_blueprint",
                    "agent_blueprint_id": "hook-smoke",
                    "definition_path": "/marketplace/hook-smoke/hooks/pre_message.py",
                    "status": "blocked",
                }
            ]
        },
    }
    result = bench.DemoResult(
        case=case,
        session_id="sess_hook",
        elapsed_s=1.0,
        message=_message(text="packaged hook smoke"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
        agent_blueprint={"active_agent_blueprint_id": "hook-smoke"},
        actions=[
            {
                "type": "agent_blueprint_hook_enable",
                "ok": True,
                "hook_id": "pre_message",
                "trust": {"trusted": True, "source": "request"},
            },
            {
                "type": "packaged_hook_probe",
                "ok": True,
                "hook_id": "pre_message",
                "semantic_events": [hook_event],
            },
        ],
    )

    assert bench._case_observed_semantic_proofs(result) == ("packaged_hook_invocation",)


def test_workspace_memory_scope_requires_structured_policy_actions() -> None:
    case = bench.DemoCase(
        case_id="workspace_memory",
        title="workspace memory",
        category="test",
        prompt="prompt",
        why="why",
        expected="expected",
        session_group="semantic",
        semantic_proofs=("workspace_memory_scope",),
    )
    result = bench.DemoResult(
        case=case,
        session_id="sess_current",
        elapsed_s=1.0,
        message=_message(text="workspace memory scope"),
        provider={"provider": "codex", "model": "gpt-5.5", "api_base": ""},
        benchmark_lane="semantic_regression",
        actions=[
            {
                "type": "workspace_memory_scope_probe",
                "ok": True,
                "current_session_id": "sess_current",
                "prior_session_id": "sess_prior",
                "other_session_id": "sess_other",
                "same_workspace_hit_session_id": "sess_prior",
                "other_workspace_hit_session_id": "",
                "checks": [
                    {
                        "name": "deny_without_intent",
                        "ok": True,
                        "policy_decision": "deny_cross_session_requires_intent",
                    },
                    {
                        "name": "allow_same_workspace_with_intent",
                        "ok": True,
                        "policy_decision": "allow_same_workspace_user_intent",
                        "hit_session_ids": ["sess_prior"],
                    },
                    {
                        "name": "deny_other_workspace_summary",
                        "ok": True,
                        "decision": "deny_other_workspace",
                    },
                ],
            }
        ],
    )

    assert bench._case_observed_semantic_proofs(result) == ("workspace_memory_scope",)


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


def test_marketplace_blueprint_case_allows_live_wrapper_agent_with_root_sync_return() -> None:
    message = _message(
        text="chrA and plasmidB reference review complete",
        route_source="live_tool_observer",
        tools=[{"name": "genomics_inspect_fasta"}],
    )
    message["parts"][0]["selected_agent"] = "genomics"
    message["metadata"]["expert_handoffs"] = [
        {
            "agent_id": "reference",
            "parent_id": "main",
            "stage": "delegate.completed",
            "metadata": {"delegation_lifecycle": "sync", "return_to": "main"},
        },
        {
            "agent_id": "main",
            "stage": "parent.resumed",
            "metadata": {"delegation_lifecycle": "sync", "resumed_from": "reference"},
        },
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
            expected_terms=("chrA", "plasmidB"),
        ),
        session_id="sess_marketplace",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_agents",
        agent_blueprint={"active_agent_blueprint_id": "genomics-review"},
    )

    assert result.passed is True
    audit = bench._provider_lane_audit([result], "marketplace_agents")
    root_row = next(
        item
        for item in audit
        if item["criterion"] == "marketplace hierarchy cases prove root sync delegation"
    )
    assert root_row["passed"] is True
    assert root_row["observed"] == 1


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


def test_artifact_case_requires_user_visible_png_reference(tmp_path: Path) -> None:
    png = tmp_path / "WWMT.CI.LY_.40_timeseries.png"
    png.write_bytes(b"png-bytes")
    message = _message(
        text="The region was resolved, but no EarthScope GNSS stations were verified.",
        tools=[
            {"name": "ndp_search_datasets"},
            {"name": "ndp_stage_resource"},
            {"name": "ndp_profile_csv_resource"},
            {
                "name": "ndp_plot_csv_timeseries",
                "result": {"path": str(png), "status": "ready"},
            },
        ],
        expert_handoffs=[
            {
                "agent_id": "synthesis",
                "status": "completed",
                "stage": "delegate.completed",
                "output_summary": f"PNG artifact: {png}",
            }
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="artifact_visibility",
            title="artifact visibility",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("png",),
            min_artifacts=1,
        ),
        session_id="sess_artifact_visibility",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.artifact_evidence[0]["exists"] is True
    assert result.passed is False


def test_final_no_data_answer_contradicting_staged_acquisition_fails(tmp_path: Path) -> None:
    png = tmp_path / "WWMT.CI.LY_.40_timeseries.png"
    png.write_bytes(b"png-bytes")
    csv = tmp_path / "WWMT.CI.LY_.40.csv"
    csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n")
    message = _message(
        text=f"No EarthScope GNSS stations or time-series verified. Plot: {png}",
        tools=[
            {"name": "ndp_search_datasets"},
            {"name": "ndp_stage_resource"},
            {"name": "ndp_profile_csv_resource"},
            {
                "name": "ndp_plot_csv_timeseries",
                "result": {"path": str(png), "status": "ready"},
            },
        ],
        expert_handoffs=[
            {
                "agent_id": "synthesis",
                "status": "completed",
                "stage": "delegate.completed",
                "output_summary": (
                    '{"workflow_state":{"acquisition":{"status":"staged",'
                    '"analysis_ready":true,"local_path":"'
                    + str(csv)
                    + '"},"artifact":{"status":"ready","path":"'
                    + str(png)
                    + '"}}}'
                ),
            }
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="contradictory_acquisition",
            title="contradictory acquisition",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("png",),
            min_artifacts=1,
        ),
        session_id="sess_contradictory_acquisition",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.passed is False


def test_visible_answer_misstating_verified_artifact_path_fails(tmp_path: Path) -> None:
    workspace_csv = tmp_path / "workspace" / ".clio" / "artifacts" / "ndp-staging" / "earthscope_converted_data.csv"
    workspace_csv.parent.mkdir(parents=True)
    workspace_csv.write_text("Site,Latitude,Longitude\nUCSF,37.763,-122.458\n", encoding="utf-8")
    wrong_csv = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "earthscope_converted_data.csv"
    message = _message(
        text=f"Staged metadata path: {wrong_csv}",
        tools=[
            {
                "name": "ndp_stage_resource",
                "args": {"output_dir": str(workspace_csv.parent)},
                "result": {"path": str(workspace_csv), "status": "staged"},
            }
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="artifact_path_integrity",
            title="artifact path integrity",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_tools=("ndp_stage_resource",),
            expected_terms=("staged",),
            min_artifacts=1,
        ),
        session_id="sess_artifact_path_integrity",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.artifact_evidence[0]["path"] == str(workspace_csv)
    assert result.passed is False


def test_final_blocker_answer_must_surface_failed_tool_calls() -> None:
    message = _message(
        text="No concrete station-specific CSV could be staged.",
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True},
            {"name": "ndp_filter_earthscope_station_catalog", "ok": True},
            {"name": "ndp_search_datasets", "ok": False},
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="hidden_tool_failure",
            title="hidden tool failure",
            category="test",
            prompt="prompt",
            why="why",
            expected="expected",
            session_group="test",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
            ),
            expected_terms=("CSV",),
            min_tool_calls=4,
        ),
        session_id="sess_hidden_tool_failure",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
    )

    assert result.passed is False


def test_earthscope_region_station_csv_must_match_requested_radius(tmp_path: Path) -> None:
    catalog = tmp_path / "earthscope_converted_data.csv"
    catalog.write_text(
        "Site,Latitude,(deg),Longitude,(deg)\n"
        "UCSF,37.76296967,-122.45815583\n"
        "WWMT,33.95531352,-116.65386073\n",
        encoding="utf-8",
    )
    station_csv = tmp_path / "WWMT.CI.LY_.40.csv"
    station_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    png = tmp_path / "WWMT.CI.LY_.40_timeseries.png"
    png.write_bytes(b"png")
    message = _message(
        text=f"Analysis-ready station CSV: {station_csv}\nPlot: {png}",
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(catalog)}},
            {"name": "ndp_filter_earthscope_station_catalog", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_region_station_mismatch",
            title="earthscope region station mismatch",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "37.77 N, 122.42 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("Analysis-ready",),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_region_station_mismatch",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.passed is False
    assert "outside 75.0 km radius" in result.failure_reasons()[0]


def test_earthscope_region_station_filter_must_use_metadata_catalog(tmp_path: Path) -> None:
    station_csv = tmp_path / "WWMT.CI.LY_.40.csv"
    station_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    png = tmp_path / "WWMT.CI.LY_.40_timeseries.png"
    png.write_bytes(b"png")
    message = _message(
        text=f"Analysis-ready station CSV: {station_csv}\nPlot: {png}",
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {
                "name": "ndp_filter_earthscope_station_catalog",
                "ok": True,
                "args": {
                    "filepath": str(station_csv),
                    "latitude": 37.77,
                    "longitude": -122.42,
                    "radius_km": 75,
                },
            },
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_region_filter_on_station_csv",
            title="earthscope region filter on station csv",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "37.77 N, 122.42 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("Analysis-ready",),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_region_filter_on_station_csv",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.passed is False
    assert "instead of EarthScope station metadata" in result.failure_reasons()[0]


def test_earthscope_positive_run_requires_scientific_final_brief(tmp_path: Path) -> None:
    catalog = tmp_path / "earthscope_converted_data.csv"
    catalog.write_text(
        "Site,Latitude,(deg),Longitude,(deg),EllipElev,(m),X,(m),Y,(m),Z,(m),Epoch,(yr),Net,Status\n"
        "MTA1,34.05522077,-118.24550778,72.6251,0,0,0,2022.7616,SCGN,ACTIVE\n",
        encoding="utf-8",
    )
    station_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    station_csv.write_text("time,east,north,up,sigEE,sigNN,sigUU\n", encoding="utf-8")
    png = tmp_path / "MTA1_time_series.png"
    png.write_bytes(b"png")
    message = _message(
        text=(
            f"The GNSS time-series plot was created from `{station_csv}`. "
            f"PNG: `{png}`. The plot visualizes 2000 rows."
        ),
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(catalog)}},
            {"name": "ndp_filter_earthscope_station_catalog", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
        expert_handoffs=[
            {
                "agent_id": "synthesis",
                "stage": "delegate.completed",
                "workflow_state": {
                    "acquisition": {"status": "staged", "analysis_ready": True},
                    "resource_candidate": {
                        "station_id": "MTA1",
                        "station_distance_km": 0.713,
                        "resource_url": (
                            "https://ds2.datacollaboratory.org/Earthscope_api_dec2024/"
                            "raw_csv/MTA1.CI.LY_.30.csv"
                        ),
                    },
                },
            }
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_positive_thin_final",
            title="earthscope positive thin final",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "34.05 N, 118.25 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_positive_thin_final",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.passed is False
    assert "visible EarthScope synthesis omitted" in result.failure_reasons()[0]
    assert "NDP source URL" in result.failure_reasons()[0]
    assert "event/data-coverage limitation" in result.failure_reasons()[0]


def test_earthscope_positive_scientific_final_brief_passes(tmp_path: Path) -> None:
    catalog = tmp_path / "earthscope_converted_data.csv"
    catalog.write_text(
        "Site,Latitude,(deg),Longitude,(deg),EllipElev,(m),X,(m),Y,(m),Z,(m),Epoch,(yr),Net,Status\n"
        "MTA1,34.05522077,-118.24550778,72.6251,0,0,0,2022.7616,SCGN,ACTIVE\n",
        encoding="utf-8",
    )
    station_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    station_csv.write_text("time,east,north,up,sigEE,sigNN,sigUU\n", encoding="utf-8")
    png = tmp_path / "MTA1_time_series.png"
    png.write_bytes(b"png")
    source_url = "https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/MTA1.CI.LY_.30.csv"
    message = _message(
        text=(
            "For the 34.05 N, 118.25 W / 75 km region, selected station MTA1 "
            f"is 0.713 km from the center. Source URL: {source_url}. "
            f"Staged CSV: `{station_csv}`. Profile: 250000 rows scanned with "
            "`time`, `east`, `north`, `up`, and uncertainty columns `sigEE`, "
            f"`sigNN`, `sigUU`. PNG artifact: `{png}`. Event-catalog limitation: "
            "this run used GNSS station evidence and did not have a live "
            "earthquake event-catalog tool."
        ),
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(catalog)}},
            {"name": "ndp_filter_earthscope_station_catalog", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
        expert_handoffs=[
            {
                "agent_id": "synthesis",
                "stage": "delegate.completed",
                "workflow_state": {
                    "acquisition": {"status": "staged", "analysis_ready": True},
                    "resource_candidate": {
                        "station_id": "MTA1",
                        "station_distance_km": 0.713,
                        "resource_url": source_url,
                    },
                },
            }
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_positive_full_final",
            title="earthscope positive full final",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "34.05 N, 118.25 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("MTA1",),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_positive_full_final",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.failure_reasons() == []
    assert result.passed is True


def test_earthscope_metadata_blocker_fails_repeated_station_search_cycle() -> None:
    message = _message(
        text=(
            "Station metadata was staged and nearby stations were found, but no "
            "station-specific GNSS CSV could be staged from NDP."
        ),
        tools=[
            {
                "name": "ndp_search_datasets",
                "ok": True,
                "args": {"resource_name": station, "resource_format": "CSV"},
                "result": {
                    "datasets": [],
                    "count": 0,
                    "search_coverage": {
                        "domain": "earthscope_gnss",
                        "station_resource_search": True,
                        "resource_name": station,
                        "resource_format": "CSV",
                    },
                },
            }
            for station in ("UCSF", "SBRB", "UCSF", "MHDL")
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_repeated_station_search",
            title="earthscope repeated station search",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "37.77 N, 122.42 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
        ),
        session_id="sess_earthscope_repeated_station_search",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.passed is False
    assert "repeated station-resource searches" in result.failure_reasons()[0]
    assert "UCSF" in result.failure_reasons()[0]


def test_earthscope_region_station_csv_requires_metadata_catalog(tmp_path: Path) -> None:
    station_csv = tmp_path / "UCSF.CI.LY_.40.csv"
    station_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    png = tmp_path / "UCSF.CI.LY_.40_timeseries.png"
    png.write_bytes(b"png")
    message = _message(
        text=f"Analysis-ready station CSV: {station_csv}\nPlot: {png}",
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_region_missing_metadata_catalog",
            title="earthscope region missing metadata catalog",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "37.77 N, 122.42 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("Analysis-ready",),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_region_missing_metadata_catalog",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.passed is False
    assert "no staged EarthScope station metadata catalog" in result.failure_reasons()[0]


def test_earthscope_region_verifier_uses_metadata_data_file_for_station_csv(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "earthscope_converted_data.csv"
    station_csv = tmp_path / ".clio" / "artifacts" / "ndp-staging" / "MTA1.CI.LY_.30.csv"
    png = tmp_path / ".clio" / "artifacts" / "plot_MTA1.CI.LY_.30.png"
    catalog.parent.mkdir(parents=True)
    png.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "Site,Latitude,(deg),Longitude,(deg)\n"
        "MTA1,34.05522077,-118.24550778\n",
        encoding="utf-8",
    )
    station_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    png.write_bytes(b"png")
    message = _message(
        text=f"Analysis-ready station CSV: {station_csv}\nPlot: {png}",
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {
                "name": "ndp_stage_resource",
                "ok": True,
                "args": {"output_dir": str(catalog.parent)},
                "result": {"path": str(catalog)},
            },
            {
                "name": "ndp_filter_earthscope_station_catalog",
                "ok": True,
                "args": {
                    "filepath": str(catalog),
                    "latitude": 34.05,
                    "longitude": -118.25,
                    "radius_km": 75,
                },
            },
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_region_station_match_from_data_files",
            title="earthscope region station match from data files",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "34.05 N, 118.25 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("Analysis-ready",),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_region_station_match_from_data_files",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert str(catalog) not in result.artifacts
    assert str(catalog) in result.data_files
    assert result.failure_reasons() == []
    assert result.passed is True


def test_earthscope_region_station_csv_within_radius_passes(tmp_path: Path) -> None:
    catalog = tmp_path / "earthscope_converted_data.csv"
    catalog.write_text(
        "Site,Latitude,(deg),Longitude,(deg)\n"
        "UCSF,37.76296967,-122.45815583\n"
        "WWMT,33.95531352,-116.65386073\n",
        encoding="utf-8",
    )
    station_csv = tmp_path / "UCSF.CI.LY_.40.csv"
    station_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    png = tmp_path / "UCSF.CI.LY_.40_timeseries.png"
    png.write_bytes(b"png")
    message = _message(
        text=f"Analysis-ready station CSV: {station_csv}\nPlot: {png}",
        tools=[
            {"name": "ndp_search_datasets", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(catalog)}},
            {"name": "ndp_filter_earthscope_station_catalog", "ok": True},
            {"name": "ndp_stage_resource", "ok": True, "result": {"path": str(station_csv)}},
            {"name": "ndp_profile_csv_resource", "ok": True},
            {"name": "ndp_plot_csv_timeseries", "ok": True, "result": {"output_path": str(png)}},
        ],
    )
    result = bench.DemoResult(
        case=bench.DemoCase(
            case_id="earthscope_region_station_match",
            title="earthscope region station match",
            category="test",
            prompt=(
                "Explore EarthScope GNSS evidence for the region centered at "
                "37.77 N, 122.42 W with a 75 km radius."
            ),
            why="why",
            expected="expected",
            session_group="test",
            agent_blueprint_id="earthscope-gnss-region-depth",
            expected_tools=(
                "ndp_search_datasets",
                "ndp_stage_resource",
                "ndp_filter_earthscope_station_catalog",
                "ndp_profile_csv_resource",
                "ndp_plot_csv_timeseries",
            ),
            expected_terms=("Analysis-ready",),
            min_artifacts=1,
        ),
        session_id="sess_earthscope_region_station_match",
        elapsed_s=1.0,
        message=message,
        provider={"provider": "argonne", "model": "openai/gpt-oss-120b", "api_base": ""},
        benchmark_lane="marketplace_earthscope",
        agent_blueprint={"active_agent_blueprint_id": "earthscope-gnss-region-depth"},
    )

    assert result.passed is True


def test_marketplace_canonical_cases_require_nonseismic_complex_hierarchy() -> None:
    cases = {
        case.case_id: case
        for case in bench._make_cases(bench._canonical_benchmark_manifest())
    }
    materials = cases["marketplace_materials_crystal_review"]
    proteomics = cases["marketplace_proteomics_mzml_review"]
    hpc = cases["marketplace_hpc_io_regression"]
    format_bridge = cases["marketplace_format_bridge_integrity"]
    terrain = cases["marketplace_terrain_pointcloud_suitability"]

    assert materials.min_expert_depth == bench._MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH
    assert materials.min_branch_count == bench._MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT
    assert materials.expected_handoff_agents == (
        "crystal_structure",
        "symmetry_quality",
    )
    assert proteomics.min_expert_depth == bench._MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH
    assert proteomics.min_branch_count == bench._MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT
    assert proteomics.expected_handoff_agents == (
        "mass_spec",
        "spectra_quality",
    )
    assert hpc.min_expert_depth == bench._MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH
    assert hpc.min_branch_count == bench._MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT
    assert hpc.expected_handoff_agents == ("trace_ingest", "regression_diff")
    assert format_bridge.min_expert_depth == bench._MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH
    assert format_bridge.min_branch_count == bench._MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT
    assert format_bridge.expected_handoff_agents == (
        "source_inspect",
        "conversion_policy",
        "integrity",
    )
    assert terrain.min_expert_depth == bench._MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH
    assert terrain.min_branch_count == bench._MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT
    assert terrain.expected_handoff_agents == (
        "terrain_derivation",
        "gridding",
        "suitability",
    )


def test_earthscope_topology_cases_use_distinct_blueprint_variants() -> None:
    cases = {
        case.case_id: case
        for case in bench._make_cases(bench._canonical_benchmark_manifest())
    }

    assert (
        cases["marketplace_earthscope_gnss_region_coordinate_mutation"].agent_blueprint_id
        == "earthscope-gnss-region"
    )
    assert (
        cases["marketplace_earthscope_gnss_region_width_topology"].agent_blueprint_id
        == "earthscope-gnss-region-width"
    )
    assert (
        cases["marketplace_earthscope_gnss_region_depth_topology"].agent_blueprint_id
        == "earthscope-gnss-region-depth"
    )
    assert "marketplace_earthscope_gnss_region_width_topology" in bench._BENCHMARK_LANES[
        "marketplace_earthscope"
    ]
    assert "marketplace_earthscope_gnss_region_depth_topology" in bench._BENCHMARK_LANES[
        "marketplace_earthscope"
    ]
    assert cases["marketplace_earthscope_gnss_region_depth_topology"].min_expert_depth >= 9
    assert cases["marketplace_earthscope_gnss_region_width_topology"].min_branch_count >= 8


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
