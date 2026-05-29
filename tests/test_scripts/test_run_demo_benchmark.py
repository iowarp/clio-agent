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

    report = bench._render_report(results, tmp_path / "evidence.jsonl")

    assert "# CLIO Claude Code Real-Provider Benchmark Report" in report
    assert "Benchmark lane: `claude_code`" in report
    assert "## Evidence Summary" in report
    assert "## Provider Lane Audit" in report
    assert "## Extended Stress Coverage Audit" in report
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
