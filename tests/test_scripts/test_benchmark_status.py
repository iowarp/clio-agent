from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmark"


def _jsonl_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_current_status_points_to_current_marketplace_hierarchy_evidence() -> None:
    status = (BENCHMARK / "CURRENT_STATUS.md").read_text(encoding="utf-8")
    complex_report = (BENCHMARK / "MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md").read_text(
        encoding="utf-8"
    )
    retry_report = (BENCHMARK / "MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md").read_text(
        encoding="utf-8"
    )
    mcp_report = (BENCHMARK / "MARKETPLACE_MCP_SCOPE_REPORT.md").read_text(
        encoding="utf-8"
    )
    mcp_enabled_report = (
        BENCHMARK / "MARKETPLACE_MCP_ENABLED_EXECUTION_REPORT.md"
    ).read_text(encoding="utf-8")
    hook_report = (BENCHMARK / "MARKETPLACE_PACKAGED_HOOK_REPORT.md").read_text(
        encoding="utf-8"
    )
    complex_rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_COMPLEX_HIERARCHY_EVIDENCE.jsonl")
    retry_rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_GEOSPATIAL_RETRY_EVIDENCE.jsonl")
    mcp_rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_MCP_SCOPE_EVIDENCE.jsonl")
    mcp_enabled_rows = _jsonl_rows(
        BENCHMARK / "MARKETPLACE_MCP_ENABLED_EXECUTION_EVIDENCE.jsonl"
    )
    hook_rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_PACKAGED_HOOK_EVIDENCE.jsonl")

    seismic = next(
        row for row in complex_rows if row["case"] == "marketplace_seismic_waveform_review"
    )
    retry = next(
        row for row in retry_rows if row["case"] == "marketplace_geospatial_field_review"
    )
    mcp = next(row for row in mcp_rows if row["case"] == "marketplace_mcp_calculator_scope")
    mcp_enabled = next(
        row
        for row in mcp_enabled_rows
        if row["case"] == "marketplace_mcp_calculator_enabled_call"
    )
    hook = next(
        row
        for row in hook_rows
        if row["case"] == "marketplace_packaged_hook_blocked_turn"
    )

    assert "MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md" in status
    assert "MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md" in status
    assert "MARKETPLACE_MCP_SCOPE_REPORT.md" in status
    assert "MARKETPLACE_MCP_ENABLED_EXECUTION_REPORT.md" in status
    assert "MARKETPLACE_PACKAGED_HOOK_REPORT.md" in status
    assert "FRESH_REAL_ORCHESTRATOR_REPORT.md" in status
    assert "historical" in status.lower()
    assert "5/6" in status
    assert "1/1" in status
    assert "single clean full-lane rerun" in status
    assert seismic["outcome"] == "pass"
    assert seismic["artifact_evidence"]
    assert all(row["exists"] for row in seismic["artifact_evidence"])
    assert retry["outcome"] == "pass"
    assert retry["tool_names"] == ["geospatial_inspect_geojson", "geospatial_inspect_geojson"]
    assert mcp["outcome"] == "pass"
    assert mcp["observed_semantic_proofs"] == ["command_mcp_skill_scope"]
    descriptor = mcp["agent_blueprint"]["mcp_descriptors"][0]
    assert descriptor["enabled"] is False
    assert descriptor["trust"] == {"policy": "explicit", "trusted": False}
    assert descriptor["tools"][0]["name"] == "calculator_add"
    assert descriptor["tools"][0]["status"] == "disabled"
    assert mcp_enabled["outcome"] == "pass"
    assert mcp_enabled["observed_semantic_proofs"] == [
        "command_mcp_skill_scope",
        "enabled_mcp_execution",
    ]
    assert [action["type"] for action in mcp_enabled["actions"]] == [
        "agent_blueprint_mcp_enable",
        "mcp_tool_call",
    ]
    assert all(action["ok"] for action in mcp_enabled["actions"])
    assert mcp_enabled["actions"][0]["ready_tools"] == ["calculator_add"]
    assert mcp_enabled["actions"][0]["trust"]["trusted"] is True
    assert mcp_enabled["actions"][1]["tool"] == "calculator_add"
    assert hook["outcome"] == "pass"
    assert hook["observed_semantic_proofs"] == ["packaged_hook_invocation"]
    assert [action["type"] for action in hook["actions"]] == [
        "agent_blueprint_hook_enable",
        "packaged_hook_probe",
    ]
    assert all(action["ok"] for action in hook["actions"])
    assert hook["actions"][0]["hook_id"] == "pre_message"
    assert hook["actions"][0]["trust"]["trusted"] is True
    blocked_events = [
        event
        for event in hook["semantic_events"]
        if event["event_type"] == "hook.pre_message.blocked"
    ]
    assert blocked_events
    handler = blocked_events[0]["payload"]["handlers"][0]
    assert handler["source"] == "agent_blueprint"
    assert handler["agent_blueprint_id"] == "hook-smoke"
    assert "NDP full SAC/PNG chain verified" not in status
    assert "Result: 5/6 clean passes" in complex_report
    assert "at least three marketplace cases prove complex hierarchy depth | 5 | 3 | pass" in complex_report
    assert "Result: 1/1 clean passes" in retry_report
    assert "main -> spatial_features -> main" in retry_report
    assert "Result: 1/1 clean passes" in mcp_report
    assert "command_mcp_skill_scope | command_mcp_skill_scope" in mcp_report
    assert "Result: 1/1 clean passes" in mcp_enabled_report
    assert "enabled_mcp_execution" in mcp_enabled_report
    assert "Result: 1/1 clean passes" in hook_report
    assert "packaged_hook_invocation" in hook_report


def test_superseded_real_orchestrator_report_is_labeled_historical() -> None:
    report = (BENCHMARK / "FRESH_REAL_ORCHESTRATOR_REPORT.md").read_text(encoding="utf-8")

    assert "Historical/superseded evidence" in report[:500]
    assert "CURRENT_STATUS.md" in report[:500]
    assert "MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md" in report[:500]
