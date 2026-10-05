"""Read durable marketplace operation outcomes after navigation or reconnection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clio_agent import paths


def operation_directory() -> Path:
    """Return the Agent-owned ledger location for this connected CLIO."""
    return paths.user_state_dir() / "blueprint-operations"


def list_blueprint_operations(app: Any, *, limit: int = 30) -> list[dict[str, Any]]:
    """Return compact outcomes without exposing blueprint contents or MCP credentials."""
    directory = operation_directory()
    if not directory.is_dir():
        return []
    active: set[str] = getattr(app.state, "blueprint_active_operations", set())
    files = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    results: list[dict[str, Any]] = []
    for path in files[:limit]:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            row = {
                key: receipt[key]
                for key in (
                    "id",
                    "label",
                    "status",
                    "target",
                    "started_at",
                    "finished_at",
                    "error",
                    "revision",
                )
                if key in receipt
            }
            if (
                row.get("status") in {"waiting_for_turns", "preparing", "applying"}
                and row["id"] not in active
            ):
                row.update(
                    status="interrupted",
                    error="CLIO stopped before recording the outcome. Inspect the installed revision before retrying.",
                )
            result = receipt.get("result") or {}
            row["installed"] = [
                {
                    "id": item.get("id", ""),
                    "identity": item.get("identity", ""),
                    "version": item.get("version", ""),
                    "checksum": (item.get("install") or {}).get("checksum", ""),
                    "runtime_checks": item.get("runtime_checks", []),
                }
                for item in result.get("installed", [])
            ]
            row["skipped"] = result.get("skipped", [])
            source = result.get("source") or {}
            if source.get("error") and not row.get("error"):
                row["error"] = source["error"]
        except (OSError, ValueError, TypeError, KeyError):
            row = {
                "id": path.stem,
                "label": "Marketplace operation",
                "status": "unknown",
                "error": "This operation receipt could not be read.",
            }
        results.append(row)
    return results
