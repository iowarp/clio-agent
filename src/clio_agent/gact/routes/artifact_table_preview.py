"""Bounded, integrity-checked table previews for registered CSV artifacts."""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.artifacts.cas import CASStore, sha256_file
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion, Custody
from clio_agent.gact.artifacts.registry import get_registry

_DEFAULT_LIMIT = 1_000
_MAX_LIMIT = 2_000
_MAX_COLUMNS = 6
_MAX_SOURCE_BYTES = 256 * 1024 * 1024


def _error(status_code: int, code: str, message: str, **details: Any) -> HTTPException:
    """Build the standard typed GACT error envelope."""

    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "error": code,
                "message": message,
                "details": details,
                "recoverable": status_code < 500,
            }
        },
    )


def _workspace_root(app: FastAPI, workspace_id: str) -> Path:
    """Resolve a registered workspace root or refuse the read."""

    workspace = app.state.workspaces.get(workspace_id)
    root = str(getattr(workspace, "root_path", "") or "") if workspace is not None else ""
    if not root:
        raise _error(
            409,
            "containment_unresolved",
            "workspace root is unresolvable; cannot preview artifact data",
            workspace_id=workspace_id,
        )
    return Path(root).expanduser().resolve(strict=False)


def _artifact_source(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
) -> Path:
    """Resolve and verify the immutable bytes behind an artifact version."""

    root = _workspace_root(app, record.workspace_id)
    recorded_sha = version.sha256
    if version.custody == Custody.CAS and recorded_sha:
        blob = CASStore(root).blob_path(recorded_sha)
        if blob.is_file():
            actual = sha256_file(blob)
            if actual != recorded_sha:
                raise _error(
                    409,
                    "integrity_violation",
                    "artifact CAS bytes do not match the immutable version hash",
                    artifact_id=version.artifact_id,
                    recorded_sha256=recorded_sha,
                    actual_sha256=actual,
                )
            return blob

    source = Path(version.path).expanduser().resolve(strict=False) if version.path else None
    if source is None or not source.is_file():
        raise _error(
            404,
            "not_found",
            "artifact bytes are not retrievable",
            artifact_id=version.artifact_id,
        )
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise _error(
            403,
            "path_outside_workspace",
            "artifact path escapes its workspace root",
            artifact_id=version.artifact_id,
        ) from exc
    if recorded_sha:
        actual = sha256_file(source)
        if actual != recorded_sha:
            raise _error(
                409,
                "integrity_violation",
                "artifact bytes do not match the immutable version hash",
                artifact_id=version.artifact_id,
                recorded_sha256=recorded_sha,
                actual_sha256=actual,
            )
    return source


def _parse_columns(raw: str) -> list[str]:
    """Parse a distinct, bounded comma-separated column selection."""

    columns = [part.strip() for part in raw.split(",") if part.strip()]
    if not 2 <= len(columns) <= _MAX_COLUMNS or len(set(columns)) != len(columns):
        raise _error(
            422,
            "invalid_request",
            "table preview requires two to six distinct columns",
            columns=columns,
        )
    return columns


def _sample_indices(total_rows: int, limit: int) -> set[int]:
    """Return evenly spaced row indices including both ends of the source."""

    if total_rows <= limit:
        return set(range(total_rows))
    if limit == 1:
        return {0}
    return {round(index * (total_rows - 1) / (limit - 1)) for index in range(limit)}


def _csv_preview(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
    columns: list[str],
    limit: int,
) -> dict[str, Any]:
    """Read an evenly sampled CSV preview with bounded memory."""

    source = _artifact_source(app, record, version)
    source_size = source.stat().st_size
    if source_size > _MAX_SOURCE_BYTES:
        raise _error(
            413,
            "artifact_too_large",
            "CSV artifact exceeds the bounded preview size",
            artifact_id=version.artifact_id,
            size_bytes=source_size,
            max_bytes=_MAX_SOURCE_BYTES,
        )
    if Path(record.name).suffix.lower() != ".csv":
        raise _error(
            415,
            "unsupported_media_type",
            "table preview currently supports registered CSV artifacts",
            artifact_id=version.artifact_id,
            name=record.name,
        )

    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            missing = [column for column in columns if column not in fieldnames]
            if missing:
                raise _error(
                    422,
                    "columns_not_found",
                    "one or more requested CSV columns do not exist",
                    artifact_id=version.artifact_id,
                    missing=missing,
                    available=fieldnames,
                )
            total_rows = sum(1 for _row in reader)

        wanted = _sample_indices(total_rows, limit)
        rows: list[dict[str, str | None]] = []
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader):
                if index in wanted:
                    rows.append({column: row.get(column) for column in columns})
    except UnicodeDecodeError as exc:
        raise _error(
            422,
            "csv_decode_failed",
            "CSV artifact is not valid UTF-8 text",
            artifact_id=version.artifact_id,
        ) from exc
    except csv.Error as exc:
        raise _error(
            422,
            "csv_parse_failed",
            "CSV artifact could not be parsed",
            artifact_id=version.artifact_id,
            detail=str(exc),
        ) from exc

    return {
        "artifact_id": version.artifact_id,
        "name": record.name,
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
        "sampled_rows": len(rows),
        "truncated": total_rows > len(rows),
    }


def register_artifact_table_preview_routes(app: FastAPI) -> None:
    """Register the bounded CSV preview endpoint used by trusted A2UI charts."""

    @app.get("/v1/artifacts/{artifact_id}/table-preview")
    async def artifact_table_preview(
        artifact_id: str,
        columns: str,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        selected = _parse_columns(columns)
        bounded_limit = max(1, min(int(limit), _MAX_LIMIT))
        registry = await asyncio.to_thread(get_registry, app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _error(
                404,
                "not_found",
                f"artifact not found: {artifact_id}",
                artifact_id=artifact_id,
            )
        record, version = found
        return await asyncio.to_thread(
            _csv_preview,
            app,
            record,
            version,
            selected,
            bounded_limit,
        )


__all__ = ["register_artifact_table_preview_routes"]
