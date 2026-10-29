"""Measurements as data: rows for ``query_log``, a file for ``export_measurements``.

The log computes no statistics. It hands the agent its measured values as rows, and
as a data file (Parquet, or CSV on request) next to a manifest listing the filter,
the Observation ids, the condition keys and the file's SHA-256, so the agent's own
code can analyze them and a claim can cite that analysis. The files are returned
under result keys that designate them as artifacts, so the registry versions and
hashes them like any other output.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent.gact.science_log.entries import EntryRejected


def rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per measurement of each Observation: its id, branch, metric, value, unit
    and each condition as ``condition.<key>``."""

    out = []
    for entry in entries:
        if entry.get("type") != "observation":
            continue
        for measurement in entry["fields"].get("measurements") or []:
            row: dict[str, Any] = {
                "observation": entry["id"],
                "branch": entry["branch"],
                "metric": measurement.get("metric", ""),
                "value": measurement.get("value"),
                "unit": measurement.get("unit", ""),
            }
            for key, value in (measurement.get("conditions") or {}).items():
                row[f"condition.{key}"] = value
            out.append(row)
    return out


def export_folder(app: Any, session_id: str) -> Path:
    """The caller's node folder in an isolated workflow run, else the per-session
    ``<workspace>/.clio/science-log/<session_id>/`` (removed with the session)."""

    session = app.state.sessions.get(session_id)
    node = str(((getattr(session, "metadata", None) or {}).get("node_folder")) or "")
    if node:
        return Path(node)
    workspaces = getattr(app.state, "workspaces", None)
    workspace = workspaces.get(session.workspace_id) if workspaces and session else None
    if workspace is None:
        raise EntryRejected("workspace_unavailable", "this session has no workspace to write to")
    return Path(workspace.root_path) / ".clio" / "science-log" / session_id


def write_export(
    folder: Path,
    selected: list[dict[str, Any]],
    filter_used: dict[str, Any],
    file_format: str,
    stem: str,
) -> dict[str, Any]:
    """Write the measurement rows of ``selected`` and their manifest; return both paths."""

    if file_format not in {"parquet", "csv"}:
        raise EntryRejected("export_format_invalid", "format is parquet or csv")
    table = rows(selected)
    if not table:
        raise EntryRejected("export_empty", "no observation matching the filter has measurements")
    columns = list(dict.fromkeys(key for row in table for key in row))
    folder.mkdir(parents=True, exist_ok=True)
    data = folder / f"{stem}.{file_format}"
    if file_format == "parquet":
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415

        values = {column: _column([row.get(column) for row in table]) for column in columns}
        pq.write_table(pa.table(values), data)
    else:
        with data.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(table)
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    manifest = {
        "file": data.name,
        "format": file_format,
        "sha256": digest,
        "rows": len(table),
        "columns": columns,
        "filter": filter_used,
        "observations": sorted({row["observation"] for row in table}),
        "condition_keys": sorted(
            column.removeprefix("condition.")
            for column in columns
            if column.startswith("condition.")
        ),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = folder / f"{stem}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return {
        "output_path": str(data),
        "saved_path": str(manifest_path),
        "sha256": digest,
        "rows": len(table),
        "observations": manifest["observations"],
        "condition_keys": manifest["condition_keys"],
    }


def _column(values: list[Any]) -> list[Any]:
    """A Parquet column holds one type: all numbers stay numbers (floats when any is a
    float), anything else becomes text; a missing value stays empty."""

    present = [value for value in values if value is not None]
    if present and all(isinstance(v, bool) for v in present):
        return values
    if present and all(isinstance(v, int | float) and not isinstance(v, bool) for v in present):
        if any(isinstance(v, float) for v in present):
            return [None if v is None else float(v) for v in values]
        return values
    return [
        None if v is None else v if isinstance(v, str) else json.dumps(v, default=str)
        for v in values
    ]


__all__ = ["export_folder", "rows", "write_export"]
