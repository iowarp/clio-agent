"""Readable dashboard filenames without changing a report family's identity."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI

from clio_agent.gact.artifacts.registry import get_registry


def dashboard_artifact_name(
    app: FastAPI, workspace_id: str, report: dict[str, Any], extension: str
) -> str:
    """Retain revision names, including legacy families, and name new reports by title."""
    designation = "dashboard-report" if extension == "json" else "dashboard-export"
    for record in get_registry(app).list_for_workspace(workspace_id):
        if any(
            version.producer.get("report_id") == report["id"]
            and version.producer.get("designation") == designation
            for version in record.versions
        ) and record.name.endswith(f".dashboard.{extension}"):
            return record.name
    slug = re.sub(r"[^\w-]+", "-", report["title"].strip(), flags=re.UNICODE).strip("-_")
    slug = slug[:96].rstrip("-_") or "dashboard"
    # Separate reports can share a title; their stable suffix keeps them distinct.
    return f"{slug}-{report['id'][:8]}.dashboard.{extension}"
