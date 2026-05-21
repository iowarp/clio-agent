"""Real-provider semantic regression tests for CLIO/GACT.

These tests intentionally exercise behavior mocks cannot validate:
real planner routing, real tool choice/arguments, real local dataset
ingestion, tool-result feedback into answers, multi-turn context, and
truthful stream/error metadata.

Run against a live ``clio-agent-gact`` with a real provider configured:

    CLIO_INTEGRATION_BASE=http://127.0.0.1:17901 \
      uv run pytest tests/test_integration_v0_2/test_real_provider_semantics.py -m integration

Set ``CLIO_REAL_AUDIT_LOG=path.jsonl`` to persist one evidence row per
case. The tests do not embed provider credentials.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import _backend, _backend_alive, post_user, turn, wait_for_assistant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _backend_alive(_backend()),
        reason=(
            "CLIO_INTEGRATION_BASE not set or backend not reachable. "
            "Boot clio-agent-gact with a real LM provider configured."
        ),
    ),
]


def _data_dir() -> Path:
    return Path(os.environ.get("CLIO_REAL_DATA_DIR", Path.cwd() / "data")).resolve()


def _data_path(name: str) -> str:
    path = _data_dir() / name
    if not path.exists():
        pytest.skip(f"real data fixture not found: {path}")
    return str(path)


def _text(message: dict[str, Any]) -> str:
    return "\n".join(str(part.get("text", "")) for part in message.get("parts", []))


def _routing_agent(message: dict[str, Any]) -> str:
    for part in message.get("parts", []):
        if part.get("type") == "routing_decision":
            return str(part.get("selected_agent", ""))
    return ""


def _tools(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get("metadata") or {}
    rows = metadata.get("tools_called") or []
    return rows if isinstance(rows, list) else []


def _tool_names(message: dict[str, Any]) -> list[str]:
    return [str(row.get("name") or row.get("tool") or "") for row in _tools(message)]


def _stream_metadata(message: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(message.get("metadata") or {})
    for part in message.get("parts", []):
        part_md = part.get("metadata") or {}
        if part_md.get("stream_source"):
            metadata.setdefault("part_stream_source", part_md.get("stream_source"))
            metadata.setdefault("part_stream_fallback", part_md.get("stream_fallback"))
    return metadata


def _assert_successful_tool_answer(
    message: dict[str, Any],
    *,
    expected_agent: str,
    expected_tool_prefix: str,
    expected_terms: tuple[str, ...],
) -> None:
    assert message.get("error_info") is None, message.get("error_info")
    routed = _routing_agent(message)
    assert expected_agent in routed, f"selected_agent={routed!r}"
    names = _tool_names(message)
    assert any(name.startswith(expected_tool_prefix) for name in names), names
    text = _text(message).lower()
    for term in expected_terms:
        assert term.lower() in text, f"{term!r} missing from answer:\n{text}"


def _assert_recorded_successful_tool_answer(
    name: str,
    http: httpx.Client,
    message: dict[str, Any],
    *,
    prompt: str,
    dataset: str | None,
    expected_agent: str,
    expected_tool_prefix: str,
    expected_terms: tuple[str, ...],
) -> None:
    _record_case(name, http, message, prompt=prompt, dataset=dataset, status="observed")
    _assert_successful_tool_answer(
        message,
        expected_agent=expected_agent,
        expected_tool_prefix=expected_tool_prefix,
        expected_terms=expected_terms,
    )


def _record_case(
    name: str,
    http: httpx.Client,
    message: dict[str, Any],
    *,
    prompt: str,
    dataset: str | None = None,
    status: str = "pass",
) -> None:
    log_path = os.environ.get("CLIO_REAL_AUDIT_LOG")
    if not log_path:
        return
    provider = {}
    try:
        provider = http.get("/v1/providers/lm").json()
    except Exception:
        provider = {}
    row = {
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
        "selected_agent": _routing_agent(message),
        "tools_called": _tools(message),
        "error_info": message.get("error_info"),
        "stream_metadata": _stream_metadata(message),
        "answer_excerpt": _text(message)[:1000],
    }
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def test_real_provider_hdf5_tool_loop(http: httpx.Client, session_id: str) -> None:
    """A real model must route to data tools, generate valid HDF5
    arguments, and incorporate the tool result into the final answer.
    """

    dataset = _data_path("atmospheric.h5")
    prompt = (
        "Use CLIO's available tools to inspect this HDF5 file. "
        f"File: {dataset}. "
        "Answer with the total dataset count and the dataset names."
    )
    assistant = turn(http, session_id, prompt, timeout=420)
    _assert_recorded_successful_tool_answer(
        "hdf5_tool_loop",
        http,
        assistant,
        prompt=prompt,
        dataset=dataset,
        expected_agent="data",
        expected_tool_prefix="hdf5_",
        expected_terms=("temperature", "pressure"),
    )


def test_real_provider_parquet_tool_loop(http: httpx.Client, session_id: str) -> None:
    """A real model must inspect Parquet schema/statistics with valid
    arguments and ground the answer in returned columns.
    """

    dataset = _data_path("measurements.parquet")
    prompt = (
        "Use CLIO tools to inspect this Parquet file and summarize its schema. "
        f"File: {dataset}. "
        "Mention the temperature, pressure, humidity, and valid columns."
    )
    assistant = turn(http, session_id, prompt, timeout=420)
    _assert_recorded_successful_tool_answer(
        "parquet_tool_loop",
        http,
        assistant,
        prompt=prompt,
        dataset=dataset,
        expected_agent="analysis",
        expected_tool_prefix="parquet_",
        expected_terms=("temperature", "pressure", "humidity", "valid"),
    )


def test_real_provider_csv_tool_loop(http: httpx.Client, session_id: str) -> None:
    """A real model must route CSV inspection through the data/analysis
    path and ground the answer in CSV headers.
    """

    dataset = _data_path("observations.csv")
    prompt = (
        "Use CLIO tools to inspect this CSV file. "
        f"File: {dataset}. "
        "List the column names and identify the temperature column."
    )
    assistant = turn(http, session_id, prompt, timeout=420)
    _assert_recorded_successful_tool_answer(
        "csv_tool_loop",
        http,
        assistant,
        prompt=prompt,
        dataset=dataset,
        expected_agent="analysis",
        expected_tool_prefix="csv_",
        expected_terms=("sample_id", "temperature_k", "pressure_pa"),
    )


def test_real_provider_multiturn_uses_previous_dataset_context(
    http: httpx.Client, session_id: str
) -> None:
    """A real model should preserve enough turn state to answer a
    follow-up about the dataset it just inspected, without repeating an
    unrelated old answer or losing the topic.
    """

    dataset = _data_path("measurements.parquet")
    first_prompt = (
        "Use CLIO tools to inspect this Parquet file. "
        f"File: {dataset}. "
        "Tell me which numeric measurement columns it contains."
    )
    first = turn(http, session_id, first_prompt, timeout=420)
    _assert_recorded_successful_tool_answer(
        "multiturn_dataset_context_setup",
        http,
        first,
        prompt=first_prompt,
        dataset=dataset,
        expected_agent="analysis",
        expected_tool_prefix="parquet_",
        expected_terms=("temperature", "pressure", "humidity"),
    )

    followup = (
        "In the dataset you just inspected, which column stores pressure? "
        "Answer with the column name only."
    )
    second = turn(http, session_id, followup, timeout=300)
    _record_case("multiturn_dataset_context", http, second, prompt=followup, dataset=dataset)
    assert second.get("error_info") is None, second.get("error_info")
    text = _text(second).lower()
    assert "pressure" in text, text
    assert "ping" not in text, text


def test_real_provider_missing_file_surfaces_tool_error(
    http: httpx.Client, session_id: str
) -> None:
    """Missing local data must surface a structured tool error rather
    than a normal-looking assistant answer.
    """

    missing = str(_data_dir() / "does-not-exist.h5")
    prompt = f"Use CLIO tools to inspect this HDF5 file: {missing}"
    assistant = turn(http, session_id, prompt, timeout=300)
    error_info = assistant.get("error_info")
    assert error_info is not None, _text(assistant)
    assert error_info["error"] == "tool_error", error_info
    details = error_info.get("details") or {}
    assert details.get("tool") in {"hdf5_analyze_file", "hdf5_list_datasets"}
    assert (details.get("tool_error") or {}).get("code") == "file_not_found"
    assert not _text(assistant).strip(), "error turns must not hide behind normal answer text"
    _record_case("missing_file_tool_error", http, assistant, prompt=prompt, dataset=missing)


def test_real_provider_visualization_tool_loop(http: httpx.Client, session_id: str) -> None:
    """A real model must select the visualization path, generate valid
    plotting arguments, and return a grounded artifact reference.
    """

    dataset = _data_path("measurements.parquet")
    prompt = (
        "Use CLIO visualization tools to create a summary dashboard for this Parquet file. "
        f"File: {dataset}. "
        "Return the chart file path and briefly say what the dashboard contains."
    )
    assistant = turn(http, session_id, prompt, timeout=520)
    _assert_recorded_successful_tool_answer(
        "visualization_tool_loop",
        http,
        assistant,
        prompt=prompt,
        dataset=dataset,
        expected_agent="visualization",
        expected_tool_prefix="plot_",
        expected_terms=("summary", ".png"),
    )


def test_real_provider_endpoint_reports_effective_model(http: httpx.Client) -> None:
    """The live backend should report which real provider/model is
    configured so evidence logs are attributable.
    """

    prompt = "GET /v1/providers/lm"
    body = http.get("/v1/providers/lm").json()
    _record_case(
        "provider_endpoint_effective_config",
        http,
        {"metadata": {"provider_endpoint": body}, "parts": [], "error_info": None},
        prompt=prompt,
    )
    assert body["configured"] is True, body
    assert body["provider"], body
    assert body["model"], body
    assert body["api_base"], body


def test_real_provider_cancel_long_turn_surfaces_cancelled(
    http: httpx.Client, session_id: str
) -> None:
    """Cancelling a real-provider turn should settle as a structured
    cancellation, not as a stale or normal-looking assistant answer.
    """

    dataset = _data_path("measurements.parquet")
    prompt = (
        "Use CLIO tools to inspect this Parquet file, compute useful statistics, "
        "then write a detailed 900-word explanation of the numeric measurement columns. "
        f"File: {dataset}."
    )
    user_id = post_user(http, session_id, prompt)
    cancel = http.post(f"/v1/sessions/{session_id}/cancel")
    assert cancel.status_code == 204, cancel.text
    assistant = wait_for_assistant(http, session_id, user_id, timeout=300)
    _record_case("cancellation_truth", http, assistant, prompt=prompt, dataset=dataset)

    error_info = assistant.get("error_info")
    assert error_info is not None, assistant
    assert error_info["error"] == "cancelled", error_info
    details = error_info.get("details") or {}
    assert details.get("execution_cancellation") in {
        "best_effort",
        "cooperative",
        "turn_boundary",
    }, error_info
    assert not _text(assistant).strip(), "cancelled turns must not include a normal answer"


def test_real_provider_streaming_provenance_truthful(http: httpx.Client, session_id: str) -> None:
    """A real turn must either emit live deltas or label completed
    post-hoc text with a structured fallback reason.
    """

    prompt = "Tell me a concise 120-word story about debugging a scientific data pipeline."
    post_user(http, session_id, prompt)
    delta_count = 0
    completed_payload: dict[str, Any] | None = None
    deadline = time.monotonic() + 300
    with httpx.stream(
        "GET",
        f"{http.base_url}/v1/sessions/{session_id}/events",
        timeout=300.0,
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
    metadata = completed_payload.get("metadata") or {}
    _record_case(
        "streaming_provenance_truthful",
        http,
        {"metadata": metadata, "parts": [], "error_info": completed_payload.get("error_info")},
        prompt=prompt,
        status="observed",
    )
    source = metadata.get("stream_source")
    if delta_count:
        assert source == "live", metadata
        assert "stream_fallback" not in metadata
    else:
        assert source == "batch", metadata
        fallback = metadata.get("stream_fallback") or {}
        assert fallback.get("reason"), metadata
        assert fallback.get("live_streaming") is False, metadata
