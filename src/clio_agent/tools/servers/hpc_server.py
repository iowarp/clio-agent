"""HPC performance trace tools for CLIO."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

hpc_server = FastMCP("hpc")

_KEY_VALUE_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z][A-Za-z0-9_./ -]{1,96}?)\s*(?:=|:|\s{2,})\s*"
    r"(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?P<unit>[A-Za-z/%]*)"
)
_DARSHAN_COUNTER_RE = re.compile(
    r"^\s*(?P<module>[A-Z0-9_]+)\s+(?P<rank>-?\d+)\s+(?P<record>\S+)\s+"
    r"(?P<counter>[A-Z0-9_]+)\s+(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _normalize_key(key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    replacements = {
        "total_runtime": "runtime_s",
        "run_time": "runtime_s",
        "runtime": "runtime_s",
        "nprocs": "nprocs",
        "number_of_processes": "nprocs",
    }
    return replacements.get(normalized, normalized)


def _as_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_add(metrics: dict[str, float], key: str, value: float) -> None:
    metrics[key] = metrics.get(key, 0.0) + value


def _metric_set(metrics: dict[str, float], key: str, value: float) -> None:
    if key in metrics and key.endswith(("_time", "_time_s", "_bytes", "_ops", "_calls")):
        _metric_add(metrics, key, value)
    else:
        metrics[key] = value


def _parse_labeled_line(line: str, metrics: dict[str, float]) -> bool:
    match = _KEY_VALUE_RE.match(line)
    if not match:
        return False
    key = _normalize_key(match.group("key"))
    value = _as_number(match.group("value"))
    if value is None:
        return False
    unit = match.group("unit").lower()
    if unit in {"ms", "millisecond", "milliseconds"}:
        value /= 1000.0
        if not key.endswith("_s"):
            key = f"{key}_s"
    _metric_set(metrics, key, value)
    return True


def _darshan_metric_name(module: str, counter: str) -> str:
    module_key = module.lower()
    counter_key = counter.lower()
    if counter_key.endswith("_bytes_written"):
        return f"{module_key}_write_bytes"
    if counter_key.endswith("_bytes_read"):
        return f"{module_key}_read_bytes"
    if counter_key.endswith("_f_write_time"):
        return f"{module_key}_write_time_s"
    if counter_key.endswith("_f_read_time"):
        return f"{module_key}_read_time_s"
    if counter_key.endswith("_f_meta_time"):
        return f"{module_key}_metadata_time_s"
    if "INDEP_WRITES" in counter:
        return f"{module_key}_independent_writes"
    if "COLL_WRITES" in counter:
        return f"{module_key}_collective_writes"
    if "INDEP_READS" in counter:
        return f"{module_key}_independent_reads"
    if "COLL_READS" in counter:
        return f"{module_key}_collective_reads"
    if counter_key.endswith("_writes"):
        return f"{module_key}_write_ops"
    if counter_key.endswith("_reads"):
        return f"{module_key}_read_ops"
    return f"{module_key}_{counter_key}"


def _parse_darshan_counter(line: str, metrics: dict[str, float]) -> bool:
    match = _DARSHAN_COUNTER_RE.match(line)
    if not match:
        return False
    value = _as_number(match.group("value"))
    if value is None:
        return False
    _metric_add(metrics, _darshan_metric_name(match.group("module"), match.group("counter")), value)
    return True


def _extract_transfer_sizes(line: str, sizes: list[float]) -> None:
    lowered = line.lower()
    if "transfer" not in lowered and "write size" not in lowered and "read size" not in lowered:
        return
    for number in _NUMBER_RE.findall(line):
        value = _as_number(number)
        if value is not None and value > 0:
            sizes.append(value)


def _phase_from_metrics(metrics: dict[str, float]) -> dict[str, float]:
    write_time = sum(value for key, value in metrics.items() if "write_time" in key)
    read_time = sum(value for key, value in metrics.items() if "read_time" in key)
    metadata_time = sum(value for key, value in metrics.items() if "metadata_time" in key)
    write_bytes = sum(value for key, value in metrics.items() if "write_bytes" in key)
    read_bytes = sum(value for key, value in metrics.items() if "read_bytes" in key)
    write_ops = sum(value for key, value in metrics.items() if "write_ops" in key or "writes" in key)
    read_ops = sum(value for key, value in metrics.items() if "read_ops" in key or "reads" in key)
    return {
        "write_time_s": round(write_time, 6),
        "read_time_s": round(read_time, 6),
        "metadata_time_s": round(metadata_time, 6),
        "write_bytes": round(write_bytes, 6),
        "read_bytes": round(read_bytes, 6),
        "write_ops": round(write_ops, 6),
        "read_ops": round(read_ops, 6),
    }


def _summarize_patterns(metrics: dict[str, float], transfer_sizes: list[float]) -> dict[str, Any]:
    independent_writes = sum(value for key, value in metrics.items() if "independent_writes" in key)
    collective_writes = sum(value for key, value in metrics.items() if "collective_writes" in key)
    write_ops = sum(value for key, value in metrics.items() if "write_ops" in key)
    if not write_ops:
        write_ops = independent_writes + collective_writes
    write_bytes = sum(value for key, value in metrics.items() if "write_bytes" in key)
    avg_write_size = write_bytes / write_ops if write_ops else 0.0
    return {
        "independent_writes": round(independent_writes, 6),
        "collective_writes": round(collective_writes, 6),
        "collective_write_fraction": round(collective_writes / (independent_writes + collective_writes), 6)
        if independent_writes + collective_writes
        else None,
        "average_write_size_bytes": round(avg_write_size, 6) if avg_write_size else None,
        "observed_transfer_sizes": sorted({int(size) for size in transfer_sizes})[:20],
    }


def _parse_trace(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    metrics: dict[str, float] = {}
    transfer_sizes: list[float] = []
    parsed_lines = 0
    total_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        total_lines += 1
        _extract_transfer_sizes(stripped, transfer_sizes)
        if _parse_darshan_counter(stripped, metrics) or _parse_labeled_line(stripped, metrics):
            parsed_lines += 1
    lowered = text.lower()
    warnings = []
    if "truncated" in lowered or "incomplete" in lowered:
        warnings.append("trace declares truncated or incomplete content")
    if parsed_lines == 0:
        warnings.append("no recognized numeric metrics were parsed")
    runtime = metrics.get("runtime_s") or metrics.get("job_runtime_s") or metrics.get("total_runtime_s")
    if runtime is None:
        warnings.append("runtime metric not found")
    return {
        "filepath": str(path),
        "format": "darshan_text",
        "parsed_lines": parsed_lines,
        "total_nonempty_data_lines": total_lines,
        "partial": bool(warnings),
        "warnings": warnings,
        "metrics": {key: round(value, 6) for key, value in sorted(metrics.items())},
        "phase_summary": _phase_from_metrics(metrics),
        "io_patterns": _summarize_patterns(metrics, transfer_sizes),
    }


def _pct_change(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return (after - before) / abs(before) * 100.0


def _compare_metrics(base: dict[str, float], candidate: dict[str, float]) -> list[dict[str, Any]]:
    changes = []
    for key in sorted(set(base) | set(candidate)):
        before = float(base.get(key, 0.0))
        after = float(candidate.get(key, 0.0))
        delta = after - before
        pct = _pct_change(before, after)
        if abs(delta) <= 1e-12:
            continue
        changes.append(
            {
                "metric": key,
                "baseline": round(before, 6),
                "candidate": round(after, 6),
                "delta": round(delta, 6),
                "percent_change": None if pct is None else round(pct, 6),
            }
        )
    def _change_magnitude(row: dict[str, Any]) -> float:
        value = row["percent_change"] if row["percent_change"] is not None else row["delta"]
        return abs(float(value))

    changes.sort(key=lambda row: (0 if row["percent_change"] is not None else 1, -_change_magnitude(row)))
    return changes


def _root_cause(changes: list[dict[str, Any]]) -> dict[str, Any]:
    by_metric = {row["metric"]: row for row in changes}
    signals: list[str] = []
    write_time = next((row for row in changes if "write_time" in row["metric"]), None)
    runtime = by_metric.get("runtime_s") or by_metric.get("job_runtime_s")
    independent = next((row for row in changes if "independent_writes" in row["metric"]), None)
    collective = next((row for row in changes if "collective_writes" in row["metric"]), None)
    avg_size = by_metric.get("average_write_size_bytes")
    if write_time and float(write_time["delta"]) > 0:
        signals.append("write time increased")
    if runtime and float(runtime["delta"]) > 0:
        signals.append("total runtime increased")
    if independent and float(independent["delta"]) > 0:
        signals.append("independent write count increased")
    if collective and float(collective["delta"]) < 0:
        signals.append("collective write count decreased")
    if avg_size and float(avg_size["delta"]) > 0:
        signals.append("average write size increased")
    likely = "insufficient_evidence"
    if any("write time" in signal for signal in signals):
        likely = "write_path_regression"
    if independent and collective and float(independent["delta"]) > 0 and float(collective["delta"]) < 0:
        likely = "collective_to_independent_write_shift"
    return {"likely_cause": likely, "signals": signals}


@hpc_server.tool()
def parse_darshan_text(filepath: str) -> dict[str, Any]:
    """Parse a Darshan-style text report into normalized HPC I/O metrics."""
    try:
        safe_path = validate_read_path(filepath)
        return {"ok": True, **_parse_trace(safe_path)}
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


@hpc_server.tool()
def compare_darshan_traces(
    baseline_filepath: str,
    candidate_filepath: str,
    max_changes: int = 20,
) -> dict[str, Any]:
    """Compare two Darshan-style text reports and rank likely I/O regressions."""
    try:
        baseline_path = validate_read_path(baseline_filepath)
        candidate_path = validate_read_path(candidate_filepath)
        baseline = _parse_trace(baseline_path)
        candidate = _parse_trace(candidate_path)
        base_metrics = {
            **baseline["metrics"],
            **baseline["phase_summary"],
            **{
                key: value
                for key, value in baseline["io_patterns"].items()
                if isinstance(value, int | float)
            },
        }
        candidate_metrics = {
            **candidate["metrics"],
            **candidate["phase_summary"],
            **{
                key: value
                for key, value in candidate["io_patterns"].items()
                if isinstance(value, int | float)
            },
        }
        max_changes = max(1, min(int(max_changes or 20), 100))
        changes = _compare_metrics(base_metrics, candidate_metrics)
        top = changes[:max_changes]
        return {
            "ok": True,
            "baseline": baseline,
            "candidate": candidate,
            "partial": bool(baseline["partial"] or candidate["partial"]),
            "warnings": [*baseline["warnings"], *candidate["warnings"]],
            "top_changes": top,
            "top_changes_truncated": len(changes) > max_changes,
            "root_cause": _root_cause(top),
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


__all__ = ["hpc_server"]
