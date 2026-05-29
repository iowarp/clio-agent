"""Tests for seismic SAC archive tools."""

from __future__ import annotations

import importlib
import io
import math
import struct
import tarfile
from pathlib import Path

from pytest import MonkeyPatch

from clio_agent.tools.servers.sac_server import (
    compute_trace_statistics,
    fetch_earthscope_waveform,
    inspect_archive,
    plot_traces,
)

sac_module = importlib.import_module("clio_agent.tools.servers.sac_server")


def _sac_bytes(*, npts: int = 32, delta: float = 0.05) -> bytes:
    """Return a tiny little-endian binary SAC payload."""
    floats = [0.0] * 70
    ints = [0] * 40
    floats[0] = delta
    floats[5] = 0.0
    floats[6] = (npts - 1) * delta
    ints[6] = 6
    ints[9] = npts
    strings = b" " * 192
    samples = [math.sin(index / 4.0) for index in range(npts)]
    return struct.pack("<70f40i", *floats, *ints) + strings + struct.pack(f"<{npts}f", *samples)


def _sample_archive(tmp_path: Path) -> Path:
    """Create a deterministic TAR archive with three SAC members."""
    archive_path = tmp_path / "waveforms.tar"
    with tarfile.open(archive_path, "w") as archive:
        for station in ("AS01", "AS02", "AS03"):
            payload = _sac_bytes()
            info = tarfile.TarInfo(f"sample_event/SCP/2020-01-01.{station}.ScP.aligned.SAC")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return archive_path


def test_inspect_archive_reports_sac_members(tmp_path: Path) -> None:
    """Seismic inspection should list SAC members without extracting them."""
    archive_path = _sample_archive(tmp_path)

    result = inspect_archive(str(archive_path), max_members=2)

    assert result["sac_trace_count"] == 3
    assert result["members_truncated"] is True
    assert result["phases"] == ["SCP"]
    assert "AS01" in result["stations"]
    assert len(result["sample_members"]) == 2


def test_compute_trace_statistics_reads_sac_samples(tmp_path: Path) -> None:
    """Trace statistics should parse SAC headers and waveform samples."""
    archive_path = _sample_archive(tmp_path)

    result = compute_trace_statistics(str(archive_path), max_traces=2)

    assert result["sac_trace_count"] == 3
    assert result["traces_analyzed"] == 2
    first = result["traces"][0]
    assert first["station"] == "AS01"
    assert first["phase"] == "SCP"
    assert first["npts"] == 32
    assert first["delta_s"] == 0.05000000074505806
    assert first["peak_abs"] > 0.9


def test_compute_trace_statistics_rejects_oversized_direct_sac(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Direct SAC files should use the same bounded read policy as archive members."""
    sac_path = tmp_path / "too_large.sac"
    sac_path.write_bytes(_sac_bytes(npts=32))
    monkeypatch.setattr(sac_module, "_MAX_SAC_BYTES", 16)

    result = compute_trace_statistics(str(sac_path))

    assert result["error"]["code"] == "seismic_trace_statistics_failed"
    assert "per-trace limit" in result["error"]["message"]


def test_plot_traces_creates_png(tmp_path: Path) -> None:
    """Trace plotting should create a PNG artifact from SAC members."""
    archive_path = _sample_archive(tmp_path)
    output_path = tmp_path / "plot.png"

    result = plot_traces(str(archive_path), max_traces=3, output_path=str(output_path))

    assert result["traces_plotted"] == 3
    assert result["output_path"] == str(output_path)
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_fetch_earthscope_waveform_stages_valid_sac(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """EarthScope fallback staging should save inspectable SAC bytes."""

    class FakeResponse:
        status_code = 200
        url = "https://service.earthscope.org/irisws/timeseries/1/query?output=sacbl"
        text = ""

        def iter_content(self, chunk_size: int):
            del chunk_size
            yield _sac_bytes(npts=20)

    def fake_get(url, *, params, stream, timeout):
        assert "earthscope" in url
        assert params["output"] == "sacbl"
        assert stream is True
        assert timeout == (8, 45)
        return FakeResponse()

    monkeypatch.setattr(sac_module.requests, "get", fake_get)

    result = fetch_earthscope_waveform(output_dir=str(tmp_path))

    assert result["staged"] is True
    assert result["source"] == "earthscope_irisws_timeseries"
    staged_path = Path(result["path"])
    assert staged_path.exists()
    stats = compute_trace_statistics(str(staged_path))
    assert stats["traces"][0]["npts"] == 20
