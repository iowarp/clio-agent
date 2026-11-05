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

__all__ = [
    "latex_build_presentation",
    "latex_build_start",
    "latex_locate_presentation",
    "latex_outline_presentation",
]

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
    "warning": "Warning",
}
_SEVERITY = {"overfull_box": "warning", "font_warning": "info", "warning": "warning"}
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
    paper = row.get("paper") if isinstance(row.get("paper"), Mapping) else None
    if paper and paper.get("artifact_id"):
        kind = str(paper.get("document_type") or "Paper")
        members = [str(item) for item in paper.get("members") or []]
        detail = ", ".join(part for part in (kind, str(paper.get("venue") or "")) if part)
        blocks.append(
            {
                "id": "paper",
                "type": "link",
                "target": "artifact",
                "uri": str(paper["artifact_id"]),
                "label": str(paper.get("title") or paper.get("name") or kind),
            }
        )
        blocks.append(
            {
                "id": "paper-detail",
                "type": "text",
                "label": kind,
                "text": f"{detail}, version {paper.get('version')}, built from "
                f"{len(members)} source file{'' if len(members) == 1 else 's'}",
            }
        )
        untracked = [str(item) for item in paper.get("untracked") or []]
        if untracked:
            blocks.append(
                {
                    "id": "paper-untracked",
                    "type": "text",
                    "label": "Not versioned",
                    "text": "Changed while the build read them: " + ", ".join(untracked[:10]),
                    "severity": "warning",
                }
            )
    elif pdf and pdf.get("artifact_id"):
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


_NODE_NAMES = {
    "section": ("section", "sections"),
    "figure": ("figure", "figures"),
    "table": ("table", "tables"),
    "equation": ("equation", "equations"),
    "algorithm": ("algorithm", "algorithms"),
    "listing": ("listing", "listings"),
    "abstract": ("abstract", "abstracts"),
    "bibliography": ("bibliography", "bibliographies"),
}


def _folder(entry: str) -> PurePosixPath:
    return PurePosixPath(entry).parent if entry else PurePosixPath(".")


def _file_uri(entry: str, file: str) -> str:
    folder = _folder(entry)
    return str(folder / file) if str(folder) != "." else file


def _failed(action: str, entry: str, row: Mapping[str, Any], blocks: list[dict[str, Any]]):
    reason = str(row.get("error") or row.get("reason") or "The operation did not complete.")
    return {
        "action": action,
        "subject": "entry" if entry else None,
        "status": "failed",
        "summary": "Operation unsuccessful",
        "blocks": [
            *blocks,
            {
                "id": "failure",
                "type": "text",
                "label": "Operation failed",
                "text": reason,
                "severity": "error",
            },
        ],
    }


def latex_outline_presentation(
    args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """The paper's parts, each a link to its source lines, with its pages."""
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
            "action": "Outline LaTeX paper",
            "status": "degraded",
            "summary": "Result unavailable",
            "blocks": blocks,
        }
    if row.get("status") == "failed":
        return _failed("Outline LaTeX paper", entry, row, blocks)
    listed = 0

    def visit(nodes: Any, depth: int) -> None:
        nonlocal listed
        for node in nodes or []:
            if not isinstance(node, Mapping):
                continue
            if listed < MAX_LISTED:
                kind = str(node.get("kind") or "")
                title = str(node.get("title") or "")
                labels = node.get("labels") or []
                pages = sorted({int(p.get("page") or 0) for p in node.get("places") or []} - {0})
                where = f"{node.get('file')}:{node.get('start_line')}"
                text = "  " * depth + (title or kind.capitalize())
                detail = ", ".join(
                    filter(
                        None,
                        [
                            kind if title and kind != "section" else "",
                            str(labels[0]) if labels else "",
                            f"page {pages[0]}"
                            if len(pages) == 1
                            else (f"pages {pages[0]}-{pages[-1]}" if pages else ""),
                        ],
                    )
                )
                blocks.append(
                    {
                        "id": f"node-{listed}",
                        "type": "link",
                        "target": "file",
                        "uri": _file_uri(entry, str(node.get("file") or "")),
                        "line": int(node.get("start_line") or 0) or None,
                        "label": f"{text} ({detail}) at {where}"
                        if detail
                        else f"{text} at {where}",
                    }
                )
            listed += 1
            visit(node.get("children"), depth + 1)

    visit(row.get("nodes"), 0)
    if listed > MAX_LISTED:
        blocks.append(
            {
                "id": "nodes-more",
                "type": "text",
                "label": "More parts",
                "text": f"{listed - MAX_LISTED} more are in Technical details.",
            }
        )
    unresolved = row.get("unresolved_citations") or []
    if unresolved:
        blocks.append(
            {
                "id": "unresolved",
                "type": "text",
                "label": "Citations the .bib lacks",
                "text": ", ".join(str(key) for key in unresolved),
                "severity": "warning",
            }
        )
    if not row.get("synctex"):
        blocks.append(
            {
                "id": "no-synctex",
                "type": "text",
                "label": "PDF places",
                "text": "This build has no SyncTeX index, so parts have no pages.",
                "severity": "warning",
            }
        )
    counts = row.get("counts") or {}
    parts = []
    for kind in ("section", "figure", "table", "equation", "algorithm", "listing"):
        number = int(counts.get(kind) or 0)
        if number:
            singular, plural = _NODE_NAMES[kind]
            parts.append(f"{number} {singular if number == 1 else plural}")
    summary = ", ".join(parts) or ("No parts found" if not row.get("nodes") else "Outline")
    return {
        "action": "Outline LaTeX paper",
        "subject": "entry" if entry else None,
        "summary": summary,
        "blocks": [{k: v for k, v in b.items() if v is not None} for b in blocks],
    }


def latex_locate_presentation(
    args: Mapping[str, Any], result: Any, structured: Any
) -> dict[str, Any]:
    """Where a PDF point is in the source, or where a source line lands in the PDF."""
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
            "action": "Locate in LaTeX paper",
            "status": "degraded",
            "summary": "Result unavailable",
            "blocks": blocks,
        }
    if row.get("status") == "failed":
        return _failed("Locate in LaTeX paper", entry, row, blocks)
    file, line = str(row.get("file") or ""), int(row.get("line") or 0)
    node = str(row.get("node") or "")
    if row.get("direction") == "source_to_pdf":
        pages = [int(place.get("page") or 0) for place in row.get("places") or []]
        summary = f"{file}:{line} is on page {', '.join(map(str, pages))}"
        blocks.append(
            {
                "id": "source",
                "type": "link",
                "target": "file",
                "uri": _file_uri(entry, file),
                "line": line or None,
                "label": f"{file}:{line}" + (f" in {node}" if node else ""),
            }
        )
        for index, place in enumerate(row.get("places") or []):
            rect = place.get("rect") or []
            blocks.append(
                {
                    "id": f"place-{index}",
                    "type": "text",
                    "label": f"Page {place.get('page')}",
                    "text": "Box at ({:.0f}, {:.0f}), {:.0f} x {:.0f} pt".format(*rect)
                    if len(rect) == 4
                    else "",
                }
            )
    else:
        summary = f"Page {row.get('page')} is {file}:{line}"
        blocks.append(
            {
                "id": "source",
                "type": "link",
                "target": "file",
                "uri": _file_uri(entry, file),
                "line": line or None,
                "label": f"{file}:{line}" + (f" in {node}" if node else ""),
            }
        )
    return {
        "action": "Locate in LaTeX paper",
        "subject": "entry" if entry else None,
        "summary": summary,
        "blocks": [{k: v for k, v in b.items() if v is not None} for b in blocks],
    }
