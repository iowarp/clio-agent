"""Seismic waveform inspection tools for small SAC archives.

The tools intentionally cover the narrow benchmark/workflow surface CLIO can
support today: staged SAC files or TAR archives containing SAC files. They do
not pretend to support MiniSEED, SEGY, or remote object stores.
"""

from __future__ import annotations

import math
import re
import struct
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path, validate_write_path

sac_server = FastMCP("sac")

_SAC_HEADER_BYTES = 632
_MAX_SAC_BYTES = 8 * 1024 * 1024
_EARTHSCOPE_TIMESERIES_URL = "https://service.earthscope.org/irisws/timeseries/1/query"


@dataclass(frozen=True)
class SacTrace:
    """Parsed SAC trace sample and metadata."""

    member: str
    station: str
    phase: str
    npts: int
    delta_s: float
    begin_s: float
    end_s: float
    samples: tuple[float, ...]


def _tool_error(
    *,
    code: str,
    message: str,
    next_action: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return CLIO's structured tool error shape."""
    error: dict[str, Any] = {
        "type": "tool_error",
        "code": code,
        "message": message,
        "next_action": next_action,
    }
    if details:
        error["details"] = details
    return {"error": error}


def _clean_positive_int(value: int | str | None, *, default: int, max_value: int) -> int:
    """Normalize a positive integer tool argument."""
    if value in (None, ""):
        return default
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            parsed = value
        elif isinstance(value, str):
            parsed = int(value)
        else:
            return default
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, max_value))


def _clean_token(value: str | None, *, default: str, max_len: int = 16) -> str:
    """Return a bounded FDSN token suitable for service parameters and filenames."""
    text = str(value or default).strip()
    if not text:
        text = default
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "", text)[:max_len]
    return cleaned or default


def _clean_duration(value: int | str | None) -> int:
    """Return a bounded waveform fetch duration in seconds."""
    return _clean_positive_int(value, default=60, max_value=600)


def _safe_stage_filename(*parts: str) -> str:
    """Build a stable filename from bounded service parameters."""
    stem = "_".join(_clean_token(part, default="x", max_len=32) for part in parts)
    stem = re.sub(r"_+", "_", stem).strip("._") or "earthscope_waveform"
    return f"{stem}.sac"


def _normalize_member_filter(value: str | None) -> str:
    """Return a case-insensitive substring filter for archive member names."""
    return str(value or "").strip().lower()


def _is_sac_member(name: str) -> bool:
    """Return whether an archive member looks like a SAC waveform file."""
    return name.lower().endswith(".sac")


def _member_phase(member: str) -> str:
    """Infer a phase/group label from the archive path."""
    parts = [part for part in member.replace("\\", "/").split("/") if part]
    if len(parts) >= 2:
        parent = parts[-2]
        if parent:
            return parent
    stem = Path(member).stem
    bits = stem.split(".")
    return bits[-3] if len(bits) >= 3 else "unknown"


def _member_station(member: str) -> str:
    """Infer a station label from common SAC file naming conventions."""
    stem = Path(member).stem
    bits = stem.split(".")
    if len(bits) >= 3:
        return bits[-3]
    return "unknown"


def _iter_archive_sac_members(
    filepath: Path,
    *,
    member_filter: str,
) -> list[tarfile.TarInfo]:
    """Return SAC members in a TAR archive without extracting them."""
    try:
        with tarfile.open(filepath, "r:*") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and _is_sac_member(member.name)
                and (not member_filter or member_filter in member.name.lower())
            ]
    except tarfile.TarError as exc:
        raise ValueError(f"Could not read TAR archive: {exc}") from exc
    return members


def _read_archive_member(filepath: Path, member: tarfile.TarInfo) -> bytes:
    """Read one bounded member from a TAR archive."""
    if member.size > _MAX_SAC_BYTES:
        raise ValueError(
            f"SAC member {member.name!r} is {member.size} bytes, above "
            f"the per-trace limit of {_MAX_SAC_BYTES} bytes."
        )
    with tarfile.open(filepath, "r:*") as archive:
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError(f"Could not open archive member {member.name!r}.")
        return handle.read()


def _iter_sac_payloads(
    filepath: Path,
    *,
    member_filter: str,
    max_traces: int,
) -> tuple[int, list[tuple[str, bytes]]]:
    """Return total available SAC traces and bounded payloads."""
    suffix = filepath.suffix.lower()
    if suffix == ".sac":
        if member_filter and member_filter not in filepath.name.lower():
            return 0, []
        size = filepath.stat().st_size
        if size > _MAX_SAC_BYTES:
            raise ValueError(
                f"SAC file {filepath.name!r} is {size} bytes, above "
                f"the per-trace limit of {_MAX_SAC_BYTES} bytes."
            )
        return 1, [(filepath.name, filepath.read_bytes())]

    if suffix not in {".tar", ".tgz", ".gz"}:
        raise ValueError(
            f"Unsupported seismic input {filepath}. Use a .sac, .tar, .tar.gz, or .tgz file."
        )

    members = _iter_archive_sac_members(filepath, member_filter=member_filter)
    payloads = [
        (member.name, _read_archive_member(filepath, member)) for member in members[:max_traces]
    ]
    return len(members), payloads


def _unpack_sac_header(payload: bytes) -> tuple[str, tuple[float, ...], tuple[int, ...]]:
    """Unpack a SAC binary header using the plausible endian variant."""
    if len(payload) < _SAC_HEADER_BYTES:
        raise ValueError("SAC payload is smaller than the 632-byte SAC header.")
    header = payload[:440]
    data_floats = (len(payload) - _SAC_HEADER_BYTES) // 4
    candidates: list[tuple[str, tuple[float, ...], tuple[int, ...], int]] = []
    for endian in ("<", ">"):
        floats = struct.unpack(f"{endian}70f", header[:280])
        ints = struct.unpack(f"{endian}40i", header[280:440])
        npts = ints[9] if len(ints) > 9 else 0
        delta = floats[0] if floats else -1.0
        score = 0
        if npts == data_floats:
            score += 4
        if 0 < npts <= data_floats:
            score += 2
        if 0 < delta < 1000:
            score += 1
        candidates.append((endian, floats, ints, score))
    endian, floats, ints, _score = max(candidates, key=lambda item: item[3])
    return endian, floats, ints


def _parse_sac_trace(member: str, payload: bytes) -> SacTrace:
    """Parse one SAC trace from bytes."""
    endian, floats, ints = _unpack_sac_header(payload)
    available_npts = (len(payload) - _SAC_HEADER_BYTES) // 4
    header_npts = ints[9] if len(ints) > 9 else available_npts
    npts = header_npts if 0 < header_npts <= available_npts else available_npts
    if npts <= 0:
        raise ValueError(f"SAC member {member!r} contains no samples.")
    data_start = _SAC_HEADER_BYTES
    samples = struct.unpack(
        f"{endian}{npts}f",
        payload[data_start : data_start + npts * 4],
    )
    delta_s = float(floats[0]) if floats and math.isfinite(float(floats[0])) else 0.0
    begin_s = float(floats[5]) if len(floats) > 5 and math.isfinite(float(floats[5])) else 0.0
    end_s = float(floats[6]) if len(floats) > 6 and math.isfinite(float(floats[6])) else begin_s
    if end_s <= begin_s and delta_s > 0:
        end_s = begin_s + delta_s * max(0, npts - 1)
    return SacTrace(
        member=member,
        station=_member_station(member),
        phase=_member_phase(member),
        npts=npts,
        delta_s=delta_s,
        begin_s=begin_s,
        end_s=end_s,
        samples=tuple(float(value) for value in samples),
    )


def _write_bounded_response(response: requests.Response, output_path: Path) -> int:
    """Write a streaming HTTP response while enforcing the SAC byte cap."""
    total = 0
    with output_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_SAC_BYTES:
                handle.close()
                output_path.unlink(missing_ok=True)
                raise ValueError(
                    f"EarthScope waveform exceeded {_MAX_SAC_BYTES} byte SAC staging cap."
                )
            handle.write(chunk)
    return total


def _load_sac_traces(
    filepath: str,
    *,
    member_filter: str | None,
    max_traces: int,
) -> tuple[Path, int, list[SacTrace]]:
    """Load bounded SAC traces from a direct file or archive."""
    safe_path = validate_read_path(filepath)
    normalized_filter = _normalize_member_filter(member_filter)
    total, payloads = _iter_sac_payloads(
        safe_path,
        member_filter=normalized_filter,
        max_traces=max_traces,
    )
    traces = [_parse_sac_trace(member, payload) for member, payload in payloads]
    return safe_path, total, traces


@sac_server.tool()
def fetch_earthscope_waveform(
    network: str | None = "IU",
    station: str | None = "ANMO",
    location: str | None = "00",
    channel: str | None = "BHZ",
    starttime: str | None = "2010-02-27T06:30:00",
    duration: int | str | None = 60,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Fetch a bounded EarthScope waveform segment as a local SAC file.

    This is a recovery/staging tool for workflows where catalog metadata points
    at unavailable archives but the underlying public waveform service can
    provide a small concrete trace for downstream SAC inspection and plotting.
    """

    net = _clean_token(network, default="IU")
    sta = _clean_token(station, default="ANMO")
    loc = _clean_token(location, default="00", max_len=8)
    cha = _clean_token(channel, default="BHZ")
    start = str(starttime or "2010-02-27T06:30:00").strip()
    seconds = _clean_duration(duration)
    try:
        destination_dir = Path(output_dir or Path.cwd() / "tmp" / "clio-seismic-staging")
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_stage_filename("earthscope", net, sta, loc or "--", cha, start)
        output_path = validate_write_path(str(destination_dir / filename), field="output_path")
    except FilePolicyError as exc:
        return exc.to_result()

    params = {
        "net": net,
        "sta": sta,
        "loc": loc,
        "cha": cha,
        "starttime": start,
        "duration": str(seconds),
        "output": "sacbl",
    }
    try:
        response = requests.get(
            _EARTHSCOPE_TIMESERIES_URL,
            params=params,
            stream=True,
            timeout=(8, 45),
        )
        if response.status_code >= 400:
            body = response.text[:500]
            return _tool_error(
                code="earthscope_waveform_fetch_failed",
                message=f"EarthScope returned HTTP {response.status_code}: {body}",
                next_action=(
                    "Try a different station/channel/time window or use NDP metadata "
                    "to choose a more specific waveform request."
                ),
                details={"url": response.url, "status_code": response.status_code},
            )
        size_bytes = _write_bounded_response(response, output_path)
        _load_sac_traces(str(output_path), member_filter=None, max_traces=1)
    except requests.RequestException as exc:
        return _tool_error(
            code="earthscope_waveform_fetch_failed",
            message=f"Could not fetch EarthScope waveform: {exc}",
            next_action="Retry with a different public waveform source or shorter time window.",
            details={"params": params},
        )
    except (OSError, ValueError) as exc:
        return _tool_error(
            code="earthscope_waveform_stage_failed",
            message=str(exc),
            next_action="Retry with a smaller or different SAC-compatible waveform segment.",
            details={"params": params},
        )

    return {
        "staged": True,
        "path": str(output_path),
        "size_bytes": size_bytes,
        "source": "earthscope_irisws_timeseries",
        "source_url": response.url,
        "network": net,
        "station": sta,
        "location": loc,
        "channel": cha,
        "starttime": start,
        "duration_s": seconds,
        "_meta": {"tool": "fetch_earthscope_waveform", "status": "success"},
    }


def _trace_stats(trace: SacTrace) -> dict[str, Any]:
    """Return compact numeric statistics for one trace."""
    samples = trace.samples
    mean = sum(samples) / len(samples)
    variance = sum((value - mean) ** 2 for value in samples) / len(samples)
    return {
        "member": trace.member,
        "station": trace.station,
        "phase": trace.phase,
        "npts": trace.npts,
        "delta_s": trace.delta_s,
        "begin_s": trace.begin_s,
        "end_s": trace.end_s,
        "min": min(samples),
        "max": max(samples),
        "mean": mean,
        "std": math.sqrt(variance),
        "peak_abs": max(abs(value) for value in samples),
    }


@sac_server.tool()
def inspect_archive(
    filepath: str,
    member_filter: str | None = None,
    max_members: int | str | None = 12,
) -> dict[str, Any]:
    """Inspect a staged SAC file or TAR archive and summarize waveform members."""
    try:
        safe_path = validate_read_path(filepath)
        limit = _clean_positive_int(max_members, default=12, max_value=100)
        normalized_filter = _normalize_member_filter(member_filter)
        if safe_path.suffix.lower() == ".sac":
            members = (
                [safe_path.name]
                if not normalized_filter or normalized_filter in safe_path.name.lower()
                else []
            )
            sizes = [safe_path.stat().st_size] if members else []
        else:
            tar_members = _iter_archive_sac_members(safe_path, member_filter=normalized_filter)
            members = [member.name for member in tar_members]
            sizes = [member.size for member in tar_members]
        sample_members = members[:limit]
        phases = sorted({_member_phase(member) for member in members})
        stations = sorted({_member_station(member) for member in members})
        return {
            "filepath": str(safe_path),
            "sac_trace_count": len(members),
            "sample_members": sample_members,
            "sample_sizes_bytes": sizes[:limit],
            "phases": phases[:20],
            "stations": stations[:20],
            "members_truncated": len(members) > limit,
            "_meta": {"tool": "inspect_archive", "status": "success"},
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return _tool_error(
            code="seismic_archive_inspect_failed",
            message=f"Could not inspect seismic archive: {exc}",
            next_action="Verify the file is a readable SAC file or TAR archive with SAC members.",
            details={"filepath": filepath},
        )


@sac_server.tool()
def compute_trace_statistics(
    filepath: str,
    member_filter: str | None = None,
    max_traces: int | str | None = 6,
) -> dict[str, Any]:
    """Compute statistics for SAC traces in a staged file or archive."""
    try:
        limit = _clean_positive_int(max_traces, default=6, max_value=25)
        safe_path, total, traces = _load_sac_traces(
            filepath,
            member_filter=member_filter,
            max_traces=limit,
        )
        if not traces:
            return _tool_error(
                code="no_sac_traces_found",
                message="No SAC traces matched the requested file/filter.",
                next_action="Inspect the archive first and choose a member_filter that matches SAC files.",
                details={"filepath": str(safe_path), "member_filter": member_filter},
            )
        return {
            "filepath": str(safe_path),
            "sac_trace_count": total,
            "traces_analyzed": len(traces),
            "traces": [_trace_stats(trace) for trace in traces],
            "traces_truncated": total > len(traces),
            "_meta": {"tool": "compute_trace_statistics", "status": "success"},
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return _tool_error(
            code="seismic_trace_statistics_failed",
            message=f"Could not compute SAC trace statistics: {exc}",
            next_action="Inspect the archive and retry with a smaller matching member_filter.",
            details={"filepath": filepath, "member_filter": member_filter},
        )


@sac_server.tool()
def plot_traces(
    filepath: str,
    member_filter: str | None = None,
    max_traces: int | str | None = 3,
    output_path: str = "",
) -> dict[str, Any]:
    """Plot selected SAC traces from a staged file or archive to a PNG artifact."""
    start = time.time()
    try:
        limit = _clean_positive_int(max_traces, default=3, max_value=8)
        safe_path, total, traces = _load_sac_traces(
            filepath,
            member_filter=member_filter,
            max_traces=limit,
        )
        if not traces:
            return _tool_error(
                code="no_sac_traces_found",
                message="No SAC traces matched the requested file/filter.",
                next_action="Inspect the archive first and choose a member_filter that matches SAC files.",
                details={"filepath": str(safe_path), "member_filter": member_filter},
            )
        if not output_path:
            output_dir = Path.cwd() / ".clio-agent-artifacts" / "charts"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(output_dir / f"sac_traces_{safe_path.stem}.png")
        safe_output = validate_write_path(output_path)

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, max(4, 1.6 * len(traces))))
        offset = 0.0
        for trace in traces:
            samples = trace.samples
            peak = max(max(abs(value) for value in samples), 1.0)
            normalized = [value / peak + offset for value in samples]
            times = [
                trace.begin_s + index * trace.delta_s if trace.delta_s > 0 else float(index)
                for index in range(len(samples))
            ]
            label = f"{trace.station} {trace.phase}"
            ax.plot(times, normalized, linewidth=0.8, label=label)
            offset += 1.4
        ax.set_xlabel("Time (s)" if any(trace.delta_s > 0 for trace in traces) else "Sample")
        ax.set_ylabel("Normalized trace offset")
        ax.set_title(f"SAC waveform traces: {safe_path.name}")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8, loc="upper right")
        fig.savefig(safe_output, dpi=150, bbox_inches="tight")
        plt.close(fig)

        return {
            "filepath": str(safe_path),
            "output_path": str(safe_output),
            "sac_trace_count": total,
            "traces_plotted": len(traces),
            "members": [trace.member for trace in traces],
            "duration_ms": (time.time() - start) * 1000,
            "_meta": {"tool": "plot_traces", "status": "success"},
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return _tool_error(
            code="seismic_trace_plot_failed",
            message=f"Could not plot SAC traces: {exc}",
            next_action="Inspect the archive, choose fewer traces, or provide a valid output path.",
            details={"filepath": filepath, "member_filter": member_filter},
        )


__all__ = [
    "sac_server",
    "fetch_earthscope_waveform",
    "inspect_archive",
    "compute_trace_statistics",
    "plot_traces",
]
