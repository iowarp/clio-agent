"""Local-first real-provider stress benchmarks for CLIO/GACT.

These tests are not smoke tests. They exercise multi-turn scientific
workflows over generated HDF5, Parquet, and CSV datasets and record audit
evidence for provider behavior that mocks cannot validate.

Run only against a live GACT backend:

    CLIO_INTEGRATION_BASE=http://127.0.0.1:17910 \
      uv run pytest tests/test_stress_benchmark -m "integration and benchmark" -vv -s
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.create_benchmark_data import create_benchmark_data
from tests.test_integration_contract.conftest import (
    _backend,
    _backend_alive,
    post_user,
    turn,
    wait_for_assistant,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.benchmark,
    pytest.mark.skipif(
        not _backend_alive(_backend()),
        reason=(
            "CLIO_INTEGRATION_BASE not set or backend not reachable. "
            "Boot clio-agent-gact with a real local provider configured."
        ),
    ),
]


def _benchmark_dir() -> Path:
    return Path(os.environ.get("CLIO_BENCHMARK_DATA_DIR", "tmp/clio-benchmark-data")).resolve()


def _manifest() -> dict[str, Any]:
    return create_benchmark_data(_benchmark_dir())


def _text(message: dict[str, Any]) -> str:
    return "\n".join(str(part.get("text", "")) for part in message.get("parts", []))


def _routing_agent(message: dict[str, Any]) -> str:
    for part in message.get("parts", []):
        if part.get("type") == "routing_decision":
            return str(part.get("selected_agent", ""))
    return ""


def _routing_decision(message: dict[str, Any]) -> dict[str, Any]:
    for part in message.get("parts", []):
        if part.get("type") == "routing_decision":
            return dict(part)
    return {}


def _tools(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get("metadata") or {}
    rows = metadata.get("tools_called") or []
    return rows if isinstance(rows, list) else []


def _timed_turn(
    http: httpx.Client,
    session_id: str,
    prompt: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    message = turn(http, session_id, prompt, timeout=timeout)
    elapsed_s = round(time.monotonic() - started, 3)
    metadata = dict(message.get("metadata") or {})
    metadata["_benchmark_elapsed_s"] = elapsed_s
    message["metadata"] = metadata
    return message


def _tool_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("tool") or "")


def _tool_names(message: dict[str, Any]) -> list[str]:
    return [_tool_name(row) for row in _tools(message)]


def _expert_handoffs(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get("metadata") or {}
    rows = metadata.get("expert_handoffs") or []
    return rows if isinstance(rows, list) else []


def _normalize_scientific_text(text: str) -> str:
    for original, normalized in {
        "⁻⁰": "^-0",
        "⁻¹": "^-1",
        "⁻²": "^-2",
        "⁻³": "^-3",
        "⁻⁴": "^-4",
        "⁻⁵": "^-5",
        "⁻⁶": "^-6",
        "⁻⁷": "^-7",
        "⁻⁸": "^-8",
        "⁻⁹": "^-9",
    }.items():
        text = text.replace(original, normalized)
    replacements = str.maketrans(
        {
            "⁻": "^-",
            "⁰": "^0",
            "¹": "^1",
            "²": "^2",
            "³": "^3",
            "⁴": "^4",
            "⁵": "^5",
            "⁶": "^6",
            "⁷": "^7",
            "⁸": "^8",
            "⁹": "^9",
        }
    )
    return text.translate(replacements)


def _blocking_error(message: dict[str, Any]) -> dict[str, Any] | None:
    error_info = message.get("error_info")
    if not isinstance(error_info, dict):
        return None
    details = error_info.get("details")
    if (
        isinstance(details, dict)
        and details.get("partial") is True
        and details.get("stage")
        in {
            "post_observation_planning",
            "parallel_validation_recovery",
            "step_limit_after_observations",
        }
    ):
        return None
    return error_info


def _stream_metadata(message: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(message.get("metadata") or {})
    for part in message.get("parts", []):
        part_metadata = part.get("metadata") or {}
        if part_metadata.get("stream_source"):
            metadata.setdefault("part_stream_source", part_metadata.get("stream_source"))
            metadata.setdefault("part_stream_fallback", part_metadata.get("stream_fallback"))
    return metadata


def _children(http: httpx.Client, parent_session_id: str) -> list[dict[str, Any]]:
    sessions = http.get("/v1/sessions").json()["sessions"]
    return [row for row in sessions if row.get("parent_session_id") == parent_session_id]


def _child_messages(
    http: httpx.Client, child_sessions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for child in child_sessions:
        body = http.get(f"/v1/sessions/{child['id']}/messages").json()
        messages.extend(body.get("messages", []))
    return messages


def _artifact_paths(message: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for row in _tools(message):
        result = row.get("result")
        if isinstance(result, str):
            candidates.extend(re.findall(r"[A-Za-z]:\\[^\n\r]+?\.png|/[^\s]+?\.png", result))
        elif isinstance(result, dict):
            for value in result.values():
                if isinstance(value, str) and value.endswith(".png"):
                    candidates.append(value)
    candidates.extend(re.findall(r"[A-Za-z]:\\[^\n\r]+?\.png|/[^\s]+?\.png", _text(message)))
    return candidates


def _clio_kit_stdio_spec(server_name: str) -> dict[str, Any]:
    """Return a stdio MCP launch spec matching CLIO's NDP wrapper resolution."""
    configured = os.environ.get("CLIO_KIT_PATH", "").strip()
    local_path = Path(configured).expanduser() if configured else Path("../clio-kit")
    local_path = local_path.resolve()
    if local_path.exists():
        return {
            "command": "uv",
            "args": [
                "--directory",
                str(local_path),
                "run",
                "clio-kit",
                "mcp-server",
                server_name,
            ],
        }
    configured_command = os.environ.get("CLIO_KIT_COMMAND", "").strip()
    if configured_command:
        parts = shlex.split(configured_command)
        if parts:
            return {"command": parts[0], "args": [*parts[1:], "mcp-server", server_name]}
    path_command = shutil.which("clio-kit")
    if path_command:
        return {"command": path_command, "args": ["mcp-server", server_name]}
    return {
        "command": "uvx",
        "args": ["--from", "clio-kit", "clio-kit", "mcp-server", server_name],
    }


def _external_mcp_message(
    *,
    elapsed_s: float,
    server_name: str,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build an audit-compatible pseudo-message for direct external MCP calls."""
    return {
        "parts": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
        "metadata": {
            "_benchmark_elapsed_s": elapsed_s,
            "tools_called": [
                {
                    "name": f"{server_name}.{tool_name}",
                    "args": args,
                    "result": result,
                    "ok": True,
                    "telemetry_source": "external_mcp",
                }
            ],
            "stream_source": "batch",
        },
        "error_info": None,
    }


def _record_case(
    name: str,
    http: httpx.Client,
    message: dict[str, Any],
    *,
    prompt: str,
    dataset: str | None,
    scenario: str,
    status: str,
    child_sessions: list[dict[str, Any]] | None = None,
    caveats: list[str] | None = None,
) -> None:
    log_path = os.environ.get("CLIO_STRESS_AUDIT_LOG")
    if not log_path:
        return
    provider = {}
    try:
        provider = http.get("/v1/providers/lm").json()
    except Exception:
        provider = {}
    row = {
        "scenario": scenario,
        "case": name,
        "status": status,
        "provider": {
            "provider": provider.get("provider"),
            "model": provider.get("model"),
            "api_base": provider.get("api_base"),
            "transport": provider.get("transport"),
        },
        "dataset": dataset,
        "prompt": prompt,
        "elapsed_s": (message.get("metadata") or {}).get("_benchmark_elapsed_s"),
        "selected_agent": _routing_agent(message),
        "routing_decision": _routing_decision(message),
        "tools_called": _tools(message),
        "expert_handoffs": _expert_handoffs(message),
        "artifacts": _artifact_paths(message),
        "nanoagents_spawned": child_sessions or [],
        "error_info": message.get("error_info"),
        "stream_metadata": _stream_metadata(message),
        "answer_excerpt": _text(message)[:1500],
        "caveats": caveats or [],
    }
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _assert_tool_answer(
    message: dict[str, Any],
    *,
    expected_agent: str | tuple[str, ...],
    expected_tool_prefix: str,
    expected_terms: tuple[str, ...],
) -> None:
    assert _blocking_error(message) is None, message.get("error_info")
    expected_agents = (expected_agent,) if isinstance(expected_agent, str) else expected_agent
    routing_agent = _routing_agent(message)
    assert any(agent in routing_agent for agent in expected_agents), routing_agent
    names = _tool_names(message)
    assert any(name.startswith(expected_tool_prefix) for name in names), names
    text = _normalize_scientific_text(_text(message)).lower()
    for term in expected_terms:
        assert term.lower() in text, f"{term!r} missing from answer:\n{text}"


def test_local_multistage_scientific_workflow_records_grounded_evidence(
    http: httpx.Client, session_id: str
) -> None:
    """Run a multi-turn workflow requiring data, analysis, CSV, and visualization tools."""
    manifest = _manifest()
    hdf5_path = manifest["hdf5"]["path"]
    parquet_path = manifest["parquet"]["path"]
    csv_path = manifest["csv"]["path"]
    scenario = "local_multistage_scientific_workflow"

    hdf5_prompt = (
        "I need to understand this fusion run before sharing it. "
        f"File: {hdf5_path}. "
        "What datasets are inside, what are their shapes, and what units or compression "
        "details should I know about?"
    )
    hdf5_answer = _timed_turn(http, session_id, hdf5_prompt, timeout=520)
    _record_case(
        "hdf5_structure",
        http,
        hdf5_answer,
        prompt=hdf5_prompt,
        dataset=hdf5_path,
        scenario=scenario,
        status="observed",
    )
    _assert_tool_answer(
        hdf5_answer,
        expected_agent="data",
        expected_tool_prefix="hdf5_",
        expected_terms=("electron_temperature", "density", "heat_flux", "eV", "m^-3", "MW/m^2"),
    )

    parquet_prompt = (
        "Profile the facility measurements in this Parquet file: "
        f"{parquet_path}. "
        "I care about the schema, row groups, and whether temperature_k, pressure_pa, "
        "humidity_pct, and anomaly_score look sane."
    )
    parquet_answer = _timed_turn(http, session_id, parquet_prompt, timeout=520)
    _record_case(
        "parquet_profile",
        http,
        parquet_answer,
        prompt=parquet_prompt,
        dataset=parquet_path,
        scenario=scenario,
        status="observed",
    )
    _assert_tool_answer(
        parquet_answer,
        expected_agent="analysis",
        expected_tool_prefix="parquet_",
        expected_terms=("temperature_k", "pressure_pa", "humidity_pct", "anomaly_score"),
    )

    csv_prompt = (
        "This event stream came with the run: "
        f"{csv_path}. "
        "What columns does it contain, and where are the status and operator_note fields?"
    )
    csv_answer = _timed_turn(http, session_id, csv_prompt, timeout=420)
    _record_case(
        "csv_schema",
        http,
        csv_answer,
        prompt=csv_prompt,
        dataset=csv_path,
        scenario=scenario,
        status="observed",
    )
    _assert_tool_answer(
        csv_answer,
        expected_agent="analysis",
        expected_tool_prefix="csv_",
        expected_terms=("event_id", "temperature_k", "pressure_pa", "status"),
    )

    visualization_prompt = (
        "Create a compact PNG dashboard from the Parquet file we just profiled. "
        "Tell me where it was saved and what the chart is summarizing."
    )
    visualization_answer = _timed_turn(http, session_id, visualization_prompt, timeout=620)
    _record_case(
        "visualization_artifact",
        http,
        visualization_answer,
        prompt=visualization_prompt,
        dataset=parquet_path,
        scenario=scenario,
        status="observed",
    )
    _assert_tool_answer(
        visualization_answer,
        expected_agent="visualization",
        expected_tool_prefix="plot_",
        expected_terms=("dashboard", ".png"),
    )
    artifacts = _artifact_paths(visualization_answer)
    assert artifacts, _text(visualization_answer)
    assert any(Path(path).exists() for path in artifacts), artifacts
    _record_case(
        "workflow_pass",
        http,
        visualization_answer,
        prompt=visualization_prompt,
        dataset=parquet_path,
        scenario=scenario,
        status="pass",
    )


def test_local_natural_cross_file_triage_uses_tool_backed_nanoagents(
    http: httpx.Client, session_id: str
) -> None:
    """Natural cross-file triage should decompose without implementation words."""
    manifest = _manifest()
    hdf5_path = manifest["hdf5"]["path"]
    parquet_path = manifest["parquet"]["path"]
    csv_path = manifest["csv"]["path"]
    adios_path = manifest["adios"]["path"]
    prompt = (
        "I have four related files from the same experiment: "
        f'{hdf5_path}, {parquet_path}, {csv_path}, and "{adios_path}". '
        "Give me a cross-file triage summary: what is in each file, whether the "
        "measurements look ready for downstream analysis, and what I should check next."
    )
    assistant = _timed_turn(http, session_id, prompt, timeout=620)
    children = _children(http, session_id)
    child_messages = _child_messages(http, children)
    child_tool_names = [
        _tool_name(row)
        for message in child_messages
        for row in (message.get("metadata") or {}).get("tools_called", []) or []
    ]
    _record_case(
        "nanoagent_parallel_tool_use",
        http,
        assistant,
        prompt=prompt,
        dataset=f"{hdf5_path};{parquet_path};{csv_path};{adios_path}",
        scenario="local_nanoagent_parallel_validation",
        status="observed",
        child_sessions=children,
    )
    assert _blocking_error(assistant) is None, assistant.get("error_info")
    text = _text(assistant).lower()
    for term in (
        "data_validator",
        "analysis_validator",
        "csv_validator",
        "adios_validator",
        "electron_temperature",
        "temperature_k",
        "operator_note",
        "adios",
    ):
        assert term in text, f"{term!r} missing from parent answer:\n{text}"
    assert children, "no nanoagent child session created"
    assert any(name.startswith("hdf5_") for name in child_tool_names), child_tool_names
    assert any(name.startswith("parquet_") for name in child_tool_names), child_tool_names
    assert any(name.startswith("csv_") for name in child_tool_names), child_tool_names
    assert any(name.startswith("adios_") for name in child_tool_names), child_tool_names


def test_local_adios_bp5_container_inspection_is_grounded(
    http: httpx.Client, session_id: str
) -> None:
    """ADIOS/BP5 inspection should be backed by real BP container metadata."""
    manifest = _manifest()
    adios_path = manifest["adios"]["path"]
    prompt = (
        "This ADIOS BP5 output came from a Gray-Scott run: "
        f'"{adios_path}". '
        "Tell me what the container looks like, whether profiling metadata is present, "
        "and what extra runtime is needed if variable-level metadata is unavailable."
    )
    assistant = _timed_turn(http, session_id, prompt, timeout=520)
    _record_case(
        "adios_bp5_container_inspection",
        http,
        assistant,
        prompt=prompt,
        dataset=adios_path,
        scenario="local_adios_bp5",
        status="observed",
    )
    _assert_tool_answer(
        assistant,
        expected_agent="data",
        expected_tool_prefix="adios_",
        expected_terms=("ADIOS", "BP5", "profiling", "transport", "ADIOS2"),
    )


def test_local_clio_kit_ndp_external_mcp_server_is_callable(
    http: httpx.Client, session_id: str
) -> None:
    """Install clio-kit's NDP MCP server through GACT and call a real NDP tool."""
    spec = _clio_kit_stdio_spec("ndp")
    started = time.monotonic()
    install = http.post(
        "/v1/mcp/servers",
        json={
            "name": "clio-kit-ndp",
            "transport": "stdio",
            **spec,
        },
        timeout=180,
    )
    assert install.status_code == 201, install.text
    installed = install.json()
    assert installed["status"] == "ready"
    assert {"list_organizations", "search_datasets", "get_dataset_details"}.issubset(
        set(installed["tools"])
    )

    server_id = installed["id"]
    args = {"name_filter": "noaa", "server": "global"}
    result: dict[str, Any] = {}
    try:
        response = http.post(
            f"/v1/mcp/servers/{server_id}/call",
            json={
                "tool": "list_organizations",
                "args": args,
                "session_id": session_id,
            },
            timeout=180,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["server_id"] == server_id
        assert body["tool"] == "list_organizations"
        assert body["is_error"] is False
        assert body["session_id"] == session_id
        text = "\n".join(row.get("text", "") for row in body.get("content", []))
        result = json.loads(text)
        organizations = result.get("organizations") or []
        assert result.get("_meta", {}).get("status") == "success"
        assert result.get("count", 0) >= 1
        assert any("noaa" in str(org).lower() for org in organizations), organizations
    finally:
        http.delete(f"/v1/mcp/servers/{server_id}")

    elapsed_s = round(time.monotonic() - started, 3)
    message = _external_mcp_message(
        elapsed_s=elapsed_s,
        server_name="clio-kit-ndp",
        tool_name="list_organizations",
        args=args,
        result=result,
    )
    _record_case(
        "clio_kit_ndp_external_mcp",
        http,
        message,
        prompt=(
            "Install the clio-kit NDP MCP server and list NOAA organizations "
            "from the global National Data Platform catalog."
        ),
        dataset="National Data Platform global catalog",
        scenario="clio_kit_ndp_external_mcp",
        status="passed",
        caveats=[
            "Direct external MCP call through GACT, not yet planner-selected by a core expert.",
        ],
    )


def test_local_ndp_catalog_discovery_is_visible_to_core_expert_path(
    http: httpx.Client, session_id: str
) -> None:
    """Natural NDP discovery should route through CLIO's core tool/expert path."""
    prompt = (
        "I need live catalog evidence for external collaborators. "
        "Find NOAA climate-related datasets in the National Data Platform catalog, "
        "include the organization evidence you used, and mention any resource formats "
        "that look useful for downstream analysis."
    )
    answer = _timed_turn(http, session_id, prompt, timeout=720)
    _record_case(
        "ndp_core_expert_catalog_discovery",
        http,
        answer,
        prompt=prompt,
        dataset="National Data Platform global catalog",
        scenario="local_ndp_core_expert_catalog_discovery",
        status="observed",
    )
    _assert_tool_answer(
        answer,
        expected_agent="data",
        expected_tool_prefix="ndp_",
        expected_terms=("National Data Platform", "noaa", "dataset"),
    )
    handoff_agents = {str(row.get("agent_id")) for row in _expert_handoffs(answer)}
    assert "ndp_catalog" in handoff_agents, _expert_handoffs(answer)
    assert any(name in _tool_names(answer) for name in ("ndp_search_datasets",)), _tool_names(
        answer
    )


def test_local_dirty_parquet_quality_review_is_grounded(
    http: httpx.Client, session_id: str
) -> None:
    """Dirty data should be reviewed with real statistics, not generic advice."""
    manifest = _manifest()
    dirty_path = manifest["parquet"]["dirty_path"]
    prompt = (
        "This Parquet export looks suspicious: "
        f"{dirty_path}. "
        "Review it for data quality problems and tell me what fields need attention "
        "before downstream analysis."
    )
    assistant = _timed_turn(http, session_id, prompt, timeout=520)
    _record_case(
        "dirty_parquet_quality_review",
        http,
        assistant,
        prompt=prompt,
        dataset=dirty_path,
        scenario="local_dirty_data_quality",
        status="observed",
    )
    _assert_tool_answer(
        assistant,
        expected_agent="analysis",
        expected_tool_prefix="parquet_",
        expected_terms=("temperature_k", "pressure_pa", "null", "quality_flag"),
    )


def test_local_error_surface_has_no_fake_answer(http: httpx.Client, session_id: str) -> None:
    """A benchmark failure path must surface structured errors without canned text."""
    missing = str(_benchmark_dir() / "missing_fusion_run.h5")
    prompt = (
        "Use CLIO tools to inspect this missing HDF5 benchmark file and report the datasets: "
        f"{missing}"
    )
    assistant = _timed_turn(http, session_id, prompt, timeout=360)
    _record_case(
        "missing_hdf5_error_surface",
        http,
        assistant,
        prompt=prompt,
        dataset=missing,
        scenario="local_error_surface",
        status="observed",
    )
    error_info = assistant.get("error_info")
    assert error_info is not None, _text(assistant)
    assert error_info["error"] == "tool_error", error_info
    assert not _text(assistant).strip(), "error turns must not include normal answer text"


def test_local_cancellation_surface_records_best_effort_boundary(
    http: httpx.Client, session_id: str
) -> None:
    """Cancellation must settle as structured cancellation, not stale answer text."""
    manifest = _manifest()
    parquet_path = manifest["parquet"]["path"]
    prompt = (
        "Use CLIO tools to profile every numeric column in this Parquet file, then write "
        "a detailed 1200-word benchmark report with caveats and recommendations. "
        f"File: {parquet_path}."
    )
    user_id = post_user(http, session_id, prompt)
    started = time.monotonic()
    cancel = http.post(f"/v1/sessions/{session_id}/cancel")
    assert cancel.status_code == 204, cancel.text
    assistant = wait_for_assistant(http, session_id, user_id, timeout=360)
    metadata = dict(assistant.get("metadata") or {})
    metadata["_benchmark_elapsed_s"] = round(time.monotonic() - started, 3)
    assistant["metadata"] = metadata
    _record_case(
        "cancellation_surface",
        http,
        assistant,
        prompt=prompt,
        dataset=parquet_path,
        scenario="local_cancellation",
        status="observed",
    )
    error_info = assistant.get("error_info")
    assert error_info is not None, assistant
    assert error_info["error"] == "cancelled", error_info
    assert not _text(assistant).strip(), "cancelled turns must not include normal answer text"


def test_local_streaming_provenance_records_live_or_batch_truth(
    http: httpx.Client,
) -> None:
    """Streaming provenance must distinguish live deltas from batch text."""
    session_id = http.post(
        "/v1/sessions",
        json={"title": "streaming provenance", "routing_mode": "chat"},
    ).json()["id"]
    prompt = (
        "In 180 words, explain why scientific workflow agents need evidence logs for "
        "routing, tool calls, artifacts, and failures."
    )
    user_id = post_user(http, session_id, prompt)
    delta_count = 0
    completed_payload: dict[str, Any] | None = None
    deadline = time.monotonic() + 360
    started = time.monotonic()
    with httpx.stream(
        "GET",
        f"{http.base_url}/v1/sessions/{session_id}/events",
        timeout=360.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            env = json.loads(line[len("data: ") :])
            if env["type"] == "message.part.delta":
                delta_count += 1
            if env["type"] == "message.completed":
                completed_payload = env["payload"]
                break
            if time.monotonic() > deadline:
                break

    assert completed_payload is not None, "message.completed never arrived"
    assistant = wait_for_assistant(http, session_id, user_id, timeout=60)
    metadata = dict(assistant.get("metadata") or {})
    metadata["_benchmark_elapsed_s"] = round(time.monotonic() - started, 3)
    assistant["metadata"] = metadata
    assert _blocking_error(assistant) is None, assistant.get("error_info")
    assert _text(assistant).strip(), assistant
    _record_case(
        "streaming_provenance",
        http,
        assistant,
        prompt=prompt,
        dataset=None,
        scenario="local_streaming_provenance",
        status="observed",
        caveats=[f"delta_count={delta_count}"],
    )
    metadata = completed_payload.get("metadata") or {}
    source = metadata.get("stream_source")
    if delta_count:
        assert source == "live", metadata
    else:
        assert source == "batch", metadata
        fallback = metadata.get("stream_fallback") or {}
        assert fallback.get("reason"), metadata
        assert fallback.get("live_streaming") is False, metadata
