"""Tests for HPC/Darshan trace tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent.tools.gateway import gateway
from clio_agent.tools.servers.hpc_server import (
    compare_darshan_traces,
    hpc_server,
    parse_darshan_text,
)


def _parse_result(result):
    data = result.data
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"raw": data}
    return {"raw": str(data)}


@pytest.fixture
def darshan_pair(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    baseline = tmp_path / "baseline.darshan.txt"
    regressed = tmp_path / "regressed.darshan.txt"
    baseline.write_text(
        "\n".join(
            [
                "# synthetic Darshan text report",
                "runtime: 100",
                "MPIIO 0 run_a MPIIO_F_WRITE_TIME 10",
                "MPIIO 0 run_a MPIIO_F_READ_TIME 2",
                "MPIIO 0 run_a MPIIO_BYTES_WRITTEN 104857600",
                "MPIIO 0 run_a MPIIO_WRITES 25600",
                "MPIIO 0 run_a MPIIO_COLL_WRITES 24000",
                "MPIIO 0 run_a MPIIO_INDEP_WRITES 1600",
                "dominant transfer size: 4096 bytes",
            ]
        ),
        encoding="utf-8",
    )
    regressed.write_text(
        "\n".join(
            [
                "# synthetic Darshan text report",
                "runtime: 118",
                "MPIIO 0 run_b MPIIO_F_WRITE_TIME 24.7",
                "MPIIO 0 run_b MPIIO_F_READ_TIME 2.1",
                "MPIIO 0 run_b MPIIO_BYTES_WRITTEN 104857600",
                "MPIIO 0 run_b MPIIO_WRITES 1600",
                "MPIIO 0 run_b MPIIO_COLL_WRITES 200",
                "MPIIO 0 run_b MPIIO_INDEP_WRITES 1400",
                "dominant transfer size: 65536 bytes",
            ]
        ),
        encoding="utf-8",
    )
    return baseline, regressed


def test_parse_darshan_text_extracts_io_metrics(darshan_pair: tuple[Path, Path]) -> None:
    baseline, _ = darshan_pair

    result = parse_darshan_text(str(baseline))

    assert result["ok"] is True
    assert result["metrics"]["runtime_s"] == 100
    assert result["phase_summary"]["write_time_s"] == 10
    assert result["phase_summary"]["write_bytes"] == 104857600
    assert result["io_patterns"]["average_write_size_bytes"] == 4096
    assert 4096 in result["io_patterns"]["observed_transfer_sizes"]


def test_compare_darshan_traces_flags_write_regression(darshan_pair: tuple[Path, Path]) -> None:
    baseline, regressed = darshan_pair

    result = compare_darshan_traces(str(baseline), str(regressed))

    assert result["ok"] is True
    changes = {row["metric"]: row for row in result["top_changes"]}
    assert changes["write_time_s"]["percent_change"] == 147
    assert changes["runtime_s"]["percent_change"] == 18
    assert changes["average_write_size_bytes"]["percent_change"] == 1500
    assert result["root_cause"]["likely_cause"] == "write_path_regression"
    assert "write time increased" in result["root_cause"]["signals"]


def test_compare_darshan_traces_surfaces_partial_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    baseline = tmp_path / "baseline.txt"
    partial = tmp_path / "partial.txt"
    baseline.write_text("runtime: 10\nMPIIO 0 a MPIIO_F_WRITE_TIME 1\n", encoding="utf-8")
    partial.write_text("TRUNCATED REPORT\nMPIIO 0 b MPIIO_F_WRITE_TIME 3\n", encoding="utf-8")

    result = compare_darshan_traces(str(baseline), str(partial))

    assert result["ok"] is True
    assert result["partial"] is True
    assert any("truncated" in warning for warning in result["warnings"])
    assert any("runtime metric not found" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_hpc_gateway_exposes_darshan_tools(darshan_pair: tuple[Path, Path]) -> None:
    baseline, regressed = darshan_pair
    async with Client(hpc_server) as client:
        parsed = await client.call_tool("parse_darshan_text", {"filepath": str(baseline)})
    async with Client(gateway) as client:
        compared = await client.call_tool(
            "hpc_compare_darshan_traces",
            {"baseline_filepath": str(baseline), "candidate_filepath": str(regressed)},
        )

    assert _parse_result(parsed)["phase_summary"]["write_time_s"] == 10
    assert _parse_result(compared)["root_cause"]["likely_cause"] == "write_path_regression"
