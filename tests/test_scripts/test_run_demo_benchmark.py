from __future__ import annotations

from scripts import run_demo_benchmark as bench


def _message(
    *,
    text: str = "ok",
    error: str | None = None,
    stream_source: str = "batch",
    tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    error_info = None
    if error is not None:
        error_info = {"error": error, "message": error, "details": {}, "recoverable": True}
    return {
        "parts": [
            {"type": "routing_decision", "selected_agent": "analysis"},
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


def test_cancelled_case_passes_only_structured_cancelled_without_text() -> None:
    result = _result("claude_cancellation_surface")

    assert result.passed is True
    assert result.outcome == "cancelled"
    row = bench._case_row(result)
    assert row["stream_source"] == "batch"
    assert row["benchmark_lane"] == "claude_code"


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
    assert "## Provider Lane Audit" in report
