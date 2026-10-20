"""Readable observations for native tools that return inventories and receipts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import PureWindowsPath
from typing import Any


def _text(key: str, label: str, value: Any) -> dict[str, Any]:
    return {"id": key, "type": "text", "label": label, "text": str(value)}


def _facts(
    key: str, label: str, row: Mapping[str, Any], fields: Mapping[str, str]
) -> dict[str, Any]:
    return _text(
        key,
        label,
        "\n".join(
            f"{name}: {row[field]}"
            for field, name in fields.items()
            if row.get(field) is not None and row[field] != ""
        ),
    )


def _link(key: str, target: str, uri: str, label: str) -> dict[str, Any]:
    return {"id": key, "type": "link", "target": target, "uri": uri, "label": label}


def _view(
    summary: str, blocks: list[dict[str, Any]], row: Mapping[str, Any], **header: Any
) -> dict[str, Any]:
    error = row.get("error")
    refused = row.get("ok") is False or row.get("status") == "failed" or bool(error)
    if refused:
        reason = error.get("message", "") if isinstance(error, Mapping) else error
        reason = (
            reason or row.get("detail") or row.get("reason") or "The operation did not complete."
        )
        blocks.append({**_text("failure", "Operation failed", reason), "severity": "error"})
        if row.get("hint"):
            blocks.append(_text("hint", "Next step", row["hint"]))
        header["status"] = "failed"
        summary = "Operation unsuccessful"
    elif not row:
        summary = "Result unavailable"
        blocks.append(
            {
                **_text("missing", "Result", "No structured result was recorded."),
                "severity": "warning",
            }
        )
        header["status"] = "degraded"
    return {"summary": summary, "blocks": blocks, **header}


def runtime_inventory(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose actual interpreter versions, requested imports and unavailable capabilities."""
    blocks = [
        _facts(
            "runtime",
            "Execution environment",
            row,
            {
                "os": "Operating system",
                "architecture": "Architecture",
                "python_version": "Python",
                "node_version": "Node.js",
            },
        )
    ]
    requested = args.get("required_imports") or []
    if requested:
        blocks.append(_text("requested", "Requested modules", ", ".join(requested)))
    checks = row.get("required_imports") or {}
    missing = []
    for module, check in checks.items():
        if isinstance(check, Mapping):
            state = str(check.get("status") or "unknown")
            if state != "ready":
                missing.append(module)
            blocks.append(
                _text(
                    f"module-{module}",
                    module,
                    ": ".join(
                        filter(None, [state, str(check.get("detail") or check.get("error") or "")])
                    ),
                )
            )
    for field, label in (
        ("python", "Python interpreter"),
        ("node", "Node.js executable"),
        ("output_directory", "Analysis output directory"),
    ):
        if row.get(field):
            blocks.append(_text(field, label, row[field]))
    if row.get("capabilities"):
        blocks.append(
            _text(
                "capabilities",
                "Document capabilities",
                "\n".join(
                    f"{name.replace('_', ' ')}: {state}"
                    for name, state in row["capabilities"].items()
                ),
            )
        )
    packages = row.get("packages") or {}
    if packages:
        blocks.append(
            _text(
                "packages",
                "Managed Python packages",
                "\n".join(
                    f"{name} {package.get('version', '')} ({str(package.get('verification', 'unverified')).replace('_', ' ')})"
                    for name, package in packages.items()
                    if isinstance(package, Mapping)
                ),
            )
        )
    summary = (
        f"{len(missing)} requested {'module' if len(missing) == 1 else 'modules'} unavailable"
        if missing
        else "Execution environment ready"
        if row.get("status") == "ready"
        else str(row.get("status") or "Environment inspected")
    )
    return _view(summary, blocks, row, **({"status": "degraded"} if missing else {}))


def document_inspection(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Name the document, requested selection and actual retained derivatives."""
    action = {
        "inspect": "Inspect document",
        "render": "Render document",
        "recalculate": "Recalculate workbook",
    }.get(str(args.get("action") or "inspect"), "Prepare document")
    path = str(args.get("path") or "")
    blocks = [_link("document", "file", path, PureWindowsPath(path).name)] if path else []
    selection = _facts(
        "selection", "Selection", args, {"pages": "Pages", "sheet": "Sheet", "cell_range": "Cells"}
    )
    if selection["text"]:
        blocks.append(selection)
    for field, label in (("manifest", "Preparation manifest"), ("content", "Extracted content")):
        if isinstance(row.get(field), str) and row[field]:
            blocks.append(_link(field, "file", row[field], label))
    for field, label in (("images", "Rendered pages"), ("contact_sheets", "Contact sheets")):
        value = row.get(field)
        if isinstance(value, list):
            blocks.append(_text(field, label, f"{len(value)} saved"))
    if row.get("path"):
        blocks.append(_link("recalculated", "file", row["path"], "Recalculated workbook"))
    for field, label in (("visual_review", "Visual review"), ("semantic_review", "Formula review")):
        if row.get(field):
            blocks.append(_text(field, label, row[field]))
    if "formula_count" in row:
        blocks.append(
            _facts(
                "calculation",
                "Workbook checks",
                row,
                {"formula_count": "Formulas", "issue_count": "Issues"},
            )
        )
    if row.get("issues"):
        blocks.append(
            _text(
                "calculation-issues",
                "Calculation issues",
                "\n".join(
                    f"{issue.get('part', '')} · {issue.get('cell', '')}: {str(issue.get('error', '')).replace('_', ' ')}"
                    for issue in row["issues"]
                ),
            )
        )
    if row.get("issues_truncated"):
        blocks.append(
            _text(
                "issues-limit",
                "Additional issues",
                "Only the first 100 calculation issues are included.",
            )
        )
    return _view(
        "Document preparation completed"
        if row.get("status") == "complete"
        else str(row.get("status") or "Document inspected"),
        blocks,
        row,
        action=action,
        subject="document" if path else None,
    )


def weather_forecast(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Show the resolved place, units and returned forecast coverage without a JSON dump."""
    weather = row.get("weather") or {}
    location = str(weather.get("location") or args.get("location") or "Forecast")
    blocks = [_text("location", location, location)]
    blocks.append(
        _facts(
            "current",
            "Current conditions",
            weather,
            {
                "condition": "Conditions",
                "temperature": "Temperature",
                "temperatureUnit": "Temperature unit",
                "windSpeed": "Wind speed",
                "windUnit": "Wind unit",
                "observedAt": "As of",
                "timeZone": "Time zone",
            },
        )
    )
    daily = weather.get("daily") or []
    hourly = weather.get("hourly") or []
    if daily:
        blocks.append(
            _text(
                "coverage",
                "Forecast coverage",
                f"{daily[0].get('date')} to {daily[-1].get('date')}; {len(daily)} days and {len(hourly)} hourly forecast values",
            )
        )
        blocks.append(
            {
                "id": "daily",
                "type": "markdown",
                "label": "Daily forecast",
                "text": "| Date | Conditions | Low | High |\n| --- | --- | --- | --- |\n"
                + "\n".join(
                    f"| {day.get('date', '')} | {day.get('condition', '')} | {day.get('low', '')} | {day.get('high', '')} |"
                    for day in daily
                ),
            }
        )
    if weather.get("source"):
        blocks.append(_text("source", "Source", weather["source"]))
    return _view(f"{len(daily)} forecast days", blocks, row, subject="location")


def surface_inspection(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """List the actual saved views or explain an inspected view's readiness and controls."""
    blocks: list[dict[str, Any]] = []
    if isinstance(row.get("surfaces"), list):
        for index, surface in enumerate(row["surfaces"]):
            blocks.append(
                _link(
                    f"surface-{index}",
                    "surface",
                    surface["surface_id"],
                    str(surface.get("title") or surface.get("component") or "Widget"),
                )
            )
        if not blocks:
            blocks.append(_text("empty", "Widgets", "No saved widgets in this conversation."))
        return _view(f"{len(row['surfaces'])} widgets found", blocks, row)
    artifact = row.get("artifact") or {}
    title = artifact.get("title") or next(
        (
            component.get("title")
            for component in row.get("components", [])
            if component.get("title")
        ),
        "Inspected widget",
    )
    identity = str(row.get("artifact_id") or row.get("surface_id") or args.get("surface_id") or "")
    if identity:
        blocks.append(
            _link(
                "widget",
                "artifact" if row.get("artifact_id") else "surface",
                identity,
                str(row.get("title") or title),
            )
        )
    viewers = row.get("viewers") or []
    for index, viewer in enumerate(viewers):
        label = str(
            viewer.get("title")
            or viewer.get("component_id")
            or viewer.get("viewer_id")
            or f"View {index + 1}"
        )
        blocks.append(
            _facts(
                f"viewer-{index}",
                label,
                viewer,
                {"ready": "Ready", "visible": "Visible", "status": "State", "error": "Error"},
            )
        )
    if row.get("controls"):
        blocks.append(
            _text(
                "controls",
                "Interactive controls",
                "\n".join(
                    f"{control.get('component_id', 'Component')} · {control.get('property', 'Control')} → {control.get('path', '')}"
                    if isinstance(control, Mapping)
                    else str(control)
                    for control in row["controls"]
                ),
            )
        )
    if row.get("definition_too_large"):
        blocks.append(_text("definition-limit", "Definition", row["detail"]))
    if row.get("viewers_omitted"):
        blocks.append(
            _text(
                "viewer-limit",
                "Additional views",
                f"{row['viewers_omitted']} not included in this bounded result",
            )
        )
    return _view(
        f"{sum(bool(viewer.get('ready')) for viewer in viewers)} of {len(viewers)} views ready"
        if viewers
        else "Widget inspected",
        blocks,
        row,
        subject="widget" if identity else None,
        **(
            {"status": "degraded"}
            if any(not v.get("ready") or v.get("error") for v in viewers)
            else {}
        ),
    )


def surface_capture(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Show the image actually captured, preserving its existing verified-file contract."""
    from clio_agent.gact.agents.native_presenters_workspace_file import workspace_file_presentation

    view = workspace_file_presentation(args, row, None)
    blocks = view["blocks"]
    if row.get("artifact_id"):
        blocks.insert(0, _link("capture", "artifact", row["artifact_id"], "Captured widget image"))
    return _view(
        "Rendered widget captured" if row.get("path") else "Capture unavailable", blocks, row
    )


def dashboard_report(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the saved dashboard and the authored definition it used."""
    blocks = []
    if row.get("artifact_id"):
        blocks.append(
            _link(
                "report",
                "artifact",
                row["artifact_id"],
                str(row.get("title") or "Dashboard report"),
            )
        )
    definition = str(row.get("definition_path") or args.get("definition_path") or "")
    if definition:
        blocks.append(_link("definition", "file", definition, "Dashboard definition"))
    return _view(
        "Dashboard report saved", blocks, row, subject="report" if row.get("artifact_id") else None
    )


def surface_deletion(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Report deletion only when the returned receipt confirms it."""
    identity = str(row.get("surface_id") or args.get("surface_id") or "")
    blocks = [_text("widget", "Widget", identity)] if identity else []
    return _view("Widget deleted" if row.get("deleted") else "Widget was not deleted", blocks, row)


def goal_started(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the scientific objective prominent instead of the scheduler's internal limits."""
    condition = str(row.get("condition") or args.get("condition") or "")
    return _view(
        "Goal started" if row.get("goal_id") else "Goal was not started",
        [_text("condition", condition, condition)],
        row,
        subject="condition",
    )


def loop_started(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the accepted follow-up work and its next scheduled execution."""
    blocks = [_text("prompt", "Follow-up work", args.get("prompt") or "")]
    blocks.append(
        _facts(
            "schedule",
            "Schedule",
            row,
            {"interval_s": "Interval in seconds", "next_fire_at": "Next iteration"},
        )
    )
    return _view(
        "Loop started" if row.get("loop_id") and not row.get("stopped") else "Loop was not started",
        blocks,
        row,
    )


def goal_wait(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Distinguish a parked goal from tasks that have already settled."""
    tasks = row.get("tasks") or []
    blocks = [
        _text(
            "policy",
            "Resume condition",
            "Any requested task settles"
            if row.get("return_when") == "any"
            else "All requested tasks settle",
        ),
        _text("tasks", "Requested tasks", "\n".join(tasks)),
    ]
    if row.get("message"):
        blocks.append(_text("next", "Next step", row["message"]))
    return _view(
        f"Waiting for {len(tasks)} tasks" if row.get("waiting") else "Tasks have settled",
        blocks,
        row,
    )


def source_opened(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose an actual connected-folder listing or the selected resource receipt."""
    path = str(
        row.get("path")
        or row.get("folder")
        or args.get("path")
        or args.get("folder")
        or "Connected folder"
    )
    blocks = [_text("source-path", path, path)]
    if row.get("resource_id"):
        name = str(row.get("name") or PureWindowsPath(path).name)
        blocks.append(_link("resource", "resource", row["resource_id"], name))
        if row.get("sha256"):
            blocks.append(_text("checksum", "SHA-256", row["sha256"]))
        return _view("File available for analysis", blocks, row, subject="resource")
    entries = row.get("entries") or []
    for index, entry in enumerate(entries):
        blocks.append(
            _facts(
                f"entry-{index}",
                str(entry.get("path") or entry.get("name") or "Entry"),
                entry,
                {"kind": "Type", "size": "Bytes", "media_type": "Format"},
            )
        )
    if not entries:
        blocks.append(_text("empty", "Folder contents", "No entries in this returned page."))
    if row.get("access"):
        blocks.append(_text("access", "Data access", str(row["access"]).replace("_", " ")))
    if "writes_propagate" in row:
        blocks.append(
            _text(
                "propagation",
                "Source changes",
                "Writes update the original source."
                if row["writes_propagate"]
                else "Writes do not update the original source.",
            )
        )
    if row.get("next_offset") is not None:
        blocks.append(
            _text("next-page", "More results", "More files are available on the next page.")
        )
    return _view(
        f"{len(entries)} folder {'entry' if len(entries) == 1 else 'entries'}",
        blocks,
        row,
        subject="source-path",
    )


def source_written(args: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the actual write receipt and whether it updated the original data."""
    path = str(row.get("path") or args.get("path") or "")
    blocks = [_text("source-path", path, path)]
    if row.get("revision"):
        blocks.append(_text("revision", "Saved revision", row["revision"]))
    propagated = row.get("writes_propagate")
    blocks.append(
        _text(
            "destination",
            "Saved to",
            "Original source" if propagated else "Workspace staging; publication is still required",
        )
    )
    return _view(
        "Source updated" if propagated else "Changes staged", blocks, row, subject="source-path"
    )


INSPECTION_PRESENTERS: dict[
    str, Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
] = {
    "runtime_inventory": runtime_inventory,
    "document_inspection": document_inspection,
    "weather_forecast": weather_forecast,
    "surface_inspection": surface_inspection,
    "surface_capture": surface_capture,
    "dashboard_report": dashboard_report,
    "surface_deletion": surface_deletion,
    "goal_started": goal_started,
    "loop_started": loop_started,
    "goal_wait": goal_wait,
    "source_opened": source_opened,
    "source_written": source_written,
}
