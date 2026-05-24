"""ADIOS/BP tool server for CLIO."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError

adios_server = FastMCP("adios")

BP_SUFFIXES = {".bp", ".bp4", ".bp5"}


def _validate_bp_path(filepath: str, *, field: str = "filepath") -> Path:
    """Validate an ADIOS BP path, allowing file or directory containers."""
    if not isinstance(filepath, str) or not filepath.strip():
        raise FilePolicyError(
            code="invalid_argument",
            message=f"{field} must be a non-empty string path.",
            field=field,
            next_action=f"Provide a non-empty {field}.",
            details={"received": filepath},
        )
    raw_path = Path(filepath).expanduser()
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    try:
        resolved = raw_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FilePolicyError(
            code="file_not_found",
            message=f"ADIOS/BP path does not exist: {raw_path}",
            field=field,
            path=str(raw_path),
            next_action="Provide an existing .bp, .bp4, or .bp5 path inside an allowed root.",
        ) from exc
    if resolved.suffix.lower() not in BP_SUFFIXES:
        raise FilePolicyError(
            code="invalid_argument",
            message=f"ADIOS/BP path must end in one of {sorted(BP_SUFFIXES)}: {resolved}",
            field=field,
            path=str(resolved),
            next_action="Use a BP container path ending in .bp, .bp4, or .bp5.",
        )
    FileAccessPolicy.from_env()._ensure_allowed(resolved, field=field)
    return resolved


def _container_members(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return BP container member metadata and total byte size."""
    if path.is_file():
        size = path.stat().st_size
        return ([{"name": path.name, "size_bytes": size, "kind": "file"}], size)

    members: list[dict[str, Any]] = []
    total_size = 0
    for member in sorted(path.rglob("*")):
        if not member.is_file():
            continue
        size = member.stat().st_size
        total_size += size
        members.append(
            {
                "name": member.relative_to(path).as_posix(),
                "size_bytes": size,
                "kind": "file",
            }
        )
    return members, total_size


def _profiling_summary(path: Path) -> dict[str, Any] | None:
    """Summarize ADIOS profiling.json if present."""
    profiling_path = path / "profiling.json" if path.is_dir() else path.with_name("profiling.json")
    if not profiling_path.exists():
        return None
    with profiling_path.open("r", encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        return {
            "path": str(profiling_path),
            "rank_count": 0,
            "transport_write_bytes": 0,
            "raw": rows,
        }

    transport_write_bytes = 0
    write_calls = 0
    open_calls = 0
    close_calls = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if not key.startswith("transport_") or not isinstance(value, dict):
                continue
            transport_write_bytes += int(value.get("wbytes") or 0)
            write = value.get("write") or {}
            open_ = value.get("open") or {}
            close = value.get("close") or {}
            if isinstance(write, dict):
                write_calls += int(write.get("nCalls") or 0)
            if isinstance(open_, dict):
                open_calls += int(open_.get("nCalls") or 0)
            if isinstance(close, dict):
                close_calls += int(close.get("nCalls") or 0)

    return {
        "path": str(profiling_path),
        "rank_count": len(rows),
        "transport_write_bytes": transport_write_bytes,
        "write_calls": write_calls,
        "open_calls": open_calls,
        "close_calls": close_calls,
    }


def _adios2_unavailable() -> dict[str, Any]:
    """Return a structured optional-dependency error for ADIOS2 reads."""
    return {
        "type": "dependency_unavailable",
        "code": "adios2_missing",
        "message": "ADIOS2 Python bindings are not installed in this CLIO environment.",
        "next_action": "Install the ADIOS2 runtime for this platform, then retry variable reads.",
        "details": {"package": "adios2"},
    }


def _inspect_variables_with_adios2(
    filepath: Path, variable_name: str | None = None
) -> dict[str, Any]:
    """Inspect BP variables through ADIOS2 when the optional dependency exists."""
    try:
        from adios2 import FileReader  # type: ignore[import-not-found]
    except Exception:
        return {"error": _adios2_unavailable()}

    with FileReader(str(filepath)) as stream:
        variables = {name: dict(info) for name, info in stream.available_variables().items()}
    if variable_name:
        if variable_name not in variables:
            return {
                "error": {
                    "type": "tool_error",
                    "code": "variable_not_found",
                    "message": f"Variable {variable_name!r} was not found in {filepath}.",
                    "next_action": "Use adios_inspect_variables without a variable filter.",
                }
            }
        variables = {variable_name: variables[variable_name]}
    return {
        "filepath": str(filepath),
        "variable_count": len(variables),
        "variables": variables,
        "source": "adios2",
    }


@adios_server.tool()
def inspect_file(filepath: str) -> dict[str, Any]:
    """Inspect an ADIOS/BP container, including members and profiling data."""
    try:
        safe_path = _validate_bp_path(filepath)
        members, total_size = _container_members(safe_path)
        profiling = _profiling_summary(safe_path)
        variables = _inspect_variables_with_adios2(safe_path)
        return {
            "filepath": str(safe_path),
            "format": safe_path.suffix.lower().lstrip(".").upper(),
            "is_directory": safe_path.is_dir(),
            "total_size_bytes": total_size,
            "member_count": len(members),
            "members": members,
            "has_profiling": profiling is not None,
            "profiling": profiling,
            "variable_count": int(variables.get("variable_count") or 0),
            "variables": variables.get("variables") or {},
            "variable_source": "adios2" if "error" not in variables else "unavailable",
            "adios2_status": variables.get("error"),
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


@adios_server.tool()
def inspect_variables(filepath: str, variable_name: str | None = None) -> dict[str, Any]:
    """Inspect variables in an ADIOS/BP file through ADIOS2."""
    try:
        safe_path = _validate_bp_path(filepath)
        return _inspect_variables_with_adios2(safe_path, variable_name)
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


@adios_server.tool()
def inspect_profiling(filepath: str) -> dict[str, Any]:
    """Read and summarize ADIOS profiling.json next to a BP container."""
    try:
        safe_path = _validate_bp_path(filepath)
        profiling = _profiling_summary(safe_path)
        if profiling is None:
            return {
                "error": {
                    "type": "tool_error",
                    "code": "profiling_not_found",
                    "message": f"No profiling.json was found for {safe_path}.",
                    "next_action": "Use a BP container that includes ADIOS profiling output.",
                }
            }
        return {"filepath": str(safe_path), "profiling": profiling}
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}
