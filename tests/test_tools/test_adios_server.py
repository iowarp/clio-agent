"""Tests for the ADIOS/BP tool server."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.tools.servers.adios_server import inspect_file, inspect_profiling, inspect_variables


def _make_bp5_container(tmp_path: Path) -> Path:
    bp_path = tmp_path / "run with spaces" / "data.bp5"
    bp_path.mkdir(parents=True)
    (bp_path / "data.0").write_bytes(b"x" * 128)
    (bp_path / "md.0").write_bytes(b"m" * 64)
    (bp_path / "md.idx").write_bytes(b"i" * 16)
    (bp_path / "mmd.0").write_bytes(b"q" * 32)
    profiling = [
        {
            "rank": 0,
            "transport_0": {
                "type": "File_POSIX",
                "wbytes": 128,
                "write": {"nCalls": 2},
                "open": {"nCalls": 1},
                "close": {"nCalls": 1},
            },
        },
        {"rank": 1},
    ]
    (bp_path / "profiling.json").write_text(json.dumps(profiling), encoding="utf-8")
    return bp_path


def test_inspect_file_reads_bp5_container_metadata(tmp_path: Path, monkeypatch) -> None:
    """BP container inspection should work even when ADIOS2 is unavailable."""
    bp_path = _make_bp5_container(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    result = inspect_file(str(bp_path))

    assert "error" not in result
    assert result["filepath"] == str(bp_path.resolve())
    assert result["format"] == "BP5"
    assert result["is_directory"] is True
    assert result["member_count"] == 5
    assert result["total_size_bytes"] == 128 + 64 + 16 + 32 + len(
        (bp_path / "profiling.json").read_bytes()
    )
    assert result["has_profiling"] is True
    assert result["profiling"]["rank_count"] == 2
    assert result["profiling"]["transport_write_bytes"] == 128
    assert result["variable_source"] in {"adios2", "unavailable"}


def test_inspect_profiling_summarizes_bp5_profile(tmp_path: Path, monkeypatch) -> None:
    """ADIOS profiling output should be available as a first-class tool result."""
    bp_path = _make_bp5_container(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    result = inspect_profiling(str(bp_path))

    assert "error" not in result
    assert result["profiling"]["rank_count"] == 2
    assert result["profiling"]["write_calls"] == 2


def test_inspect_variables_surfaces_missing_adios2_dependency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Variable reads must surface the missing optional runtime, not fake variables."""
    bp_path = _make_bp5_container(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))

    result = inspect_variables(str(bp_path))

    if "error" in result:
        assert result["error"]["code"] == "adios2_missing"
    else:
        assert result["source"] == "adios2"
