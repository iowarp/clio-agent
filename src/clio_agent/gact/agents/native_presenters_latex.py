"""Readable results for the LaTeX tools (``latex_build``, ``latex_outline``, ``latex_locate``).

The row says what was built and how it went: ``Build LaTeX paper`` on ``main.tex``, with
the page count and the problems by kind. Expansion links the PDF, lists every problem as a
link that opens its file at its line, and shows venue checks as checks. The exact result stays
in Technical details. A failed build is a failure even when a PDF was made.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

__all__ = ["latex_build_presentation", "latex_build_start"]

#: Problems shown individually; the rest are counted, never silently dropped.
MAX_LISTED = 60

_LABELS = {
    "error": "Error",
    "undefined_reference": "Undefined reference",
    "undefined_citation": "Undefined citation",
    "missing_file": "Missing file",
    "missing_package": "Missing package",
    "shell_escape_unsupported": "Needs shell escape",
    "unsupported_bibliography_backend": "Unsupported bibliography backend",
    "overfull_box": "Overfull box",
    "font_warning": "Font substitution",
}
_SEVERITY = {"overfull_box": "warning", "font_warning": "info"}
_ORDER = list(_LABELS)


def _row(result: Any, structured: Any) -> Mapping[str, Any]:
    for value in (structured, result):
        if isinstance(value, Mapping):
            return value
    return {}


_PLURALS = {
    "overfull_box": "overfull boxes",
    "font_warning": "font substitutions",
    "shell_escape_unsupported": "packages needing shell escape",
    "unsupported_bibliography_backend": "unsupported bibliography backends",
}


def _count_text(counts: Mapping[str, Any]) -> str:
    parts = []
    for kind in _ORDER:
        number = int(counts.get(kind) or 0)
        if number:
            singular = _LABELS[kind].lower()
            if kind == "shell_escape_unsupported":
                singular = "package needing shell escape"
            plural = _PLURALS.get(kind, singular + "s")
            parts.append(f"{number} {singular if number == 1 else plural}")
    return ", ".join(parts)


def latex_build_start(args: Mapping[str, Any]) -> dict[str, Any]:
    """The running row: what is being built."""
    entry = str(args.get("entry") or "")
    blocks: list[dict[str, Any]] = []
    if entry:
        blocks.append(
            {
                "id": "entry",
                "type": "link",
                "target": "file",
                "uri": entry,
                "label": PurePosixPath(entry).name,
            }
        )
    return {
        "action": "Build LaTeX paper",
        "subject": "entry" if entry else None,
        "status": "running",
        "summary": "Building",
        "blocks": blocks,
    }


def latex_build_presentation(
    args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """The build's PDF, problems by file and line, and venue checks."""
    row = _row(result, structured)
    entry = str(row.get("entry") or args.get("entry") or "")
    blocks: list[dict[str, Any]] = []
    if entry:
        blocks.append(
            {
                "id": "entry",
                "type": "link",
                "target": "file",
                "uri": entry,
                "label": PurePosixPath(entry).name,
            }
        )
    if not row:
        return {
            "action": "Build LaTeX paper",
            "subject": "entry" if entry else None,
            "status": "degraded",
            "summary": "Result unavailable",
            "blocks": [
                *blocks,
                {
                    "id": "missing",
                    "type": "text",
                    "label": "Result",
                    "text": "No structured result was recorded.",
                    "severity": "warning",
                },
            ],
        }
    pdf = row.get("pdf") if isinstance(row.get("pdf"), Mapping) else None
    if pdf and pdf.get("artifact_id"):
        blocks.append(
            {
                "id": "pdf",
                "type": "link",
                "target": "artifact",
                "uri": str(pdf["artifact_id"]),
                "label": str(pdf.get("name") or "PDF"),
            }
        )
    pages = int(row.get("page_count") or 0)
    diagnostics = [item for item in row.get("diagnostics") or [] if isinstance(item, Mapping)]
    diagnostics.sort(
        key=lambda item: _ORDER.index(item.get("kind"))
        if item.get("kind") in _ORDER
        else len(_ORDER)
    )
    for index, item in enumerate(diagnostics[:MAX_LISTED]):
        kind = str(item.get("kind") or "")
        label = _LABELS.get(kind, kind.replace("_", " ").capitalize())
        message = str(item.get("message") or "")
        file, line = str(item.get("file") or ""), int(item.get("line") or 0)
        severity = _SEVERITY.get(kind, "error")
        if file:
            folder = PurePosixPath(entry).parent if entry else PurePosixPath(".")
            blocks.append(
                {
                    "id": f"problem-{index}",
                    "type": "link",
                    "target": "file",
                    "uri": str(folder / file) if str(folder) != "." else file,
                    "line": line or None,
                    "label": f"{label} at {file}{f':{line}' if line else ''}: {message}",
                    "severity": severity,
                }
            )
        else:
            blocks.append(
                {
                    "id": f"problem-{index}",
                    "type": "text",
                    "label": label,
                    "text": message,
                    "severity": severity,
                }
            )
    if len(diagnostics) > MAX_LISTED:
        blocks.append(
            {
                "id": "problems-more",
                "type": "text",
                "label": "More problems",
                "text": f"{len(diagnostics) - MAX_LISTED} more are in Technical details.",
            }
        )
    venue = row.get("venue") if isinstance(row.get("venue"), Mapping) else None
    if venue:
        for index, check in enumerate(venue.get("checks") or []):
            if not isinstance(check, Mapping):
                continue
            state = str(check.get("status") or "")
            blocks.append(
                {
                    "id": f"venue-{index}",
                    "type": "check",
                    "label": str(check.get("rule") or ""),
                    "status": state,
                    "text": str(check.get("message") or ""),
                    "severity": "error"
                    if state == "failed"
                    else ("warning" if state == "to_check" else None),
                }
            )
    if row.get("network") and not str(row["network"]).startswith("bundle_hosts"):
        blocks.append(
            {
                "id": "network",
                "type": "text",
                "label": "Network",
                "text": str(row["network"]),
                "severity": "info",
            }
        )
    counts = _count_text(row.get("counts") or {})
    failed = row.get("status") == "failed"
    if failed and not pdf:
        summary = "No PDF" + (f": {counts}" if counts else "")
        if row.get("error") and not counts:
            blocks.append(
                {
                    "id": "failure",
                    "type": "text",
                    "label": "Build failed",
                    "text": str(row["error"]),
                    "severity": "error",
                }
            )
    else:
        summary = f"{pages} page{'' if pages == 1 else 's'}" + (f", {counts}" if counts else "")
    return {
        "action": "Build LaTeX paper",
        "subject": "entry" if entry else None,
        "status": "failed" if failed else None,
        "summary": summary,
        "blocks": [
            {key: value for key, value in block.items() if value is not None} for block in blocks
        ],
    }
