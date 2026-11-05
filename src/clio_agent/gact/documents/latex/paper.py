"""A LaTeX paper's build report, outline and click-through, shared by the tools and routes.

``latex_build``, ``latex_outline`` and ``latex_locate`` (``gact/latex_tools.py``) resolve a
workspace path in the agent's session; the LaTeX workspace's routes (``routes/latex.py``) resolve
an artifact id. Both then call these functions, so the agent and the person's view read the
same build.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
    from clio_agent.gact.documents.latex.mapping import PaperMap

__all__ = ["build_row", "counts", "locate_row", "outline_row", "paper_of"]


def counts(diagnostics: list[dict[str, Any]]) -> dict[str, int]:
    """How many diagnostics of each kind."""
    found: dict[str, int] = {}
    for item in diagnostics:
        kind = str(item.get("kind") or "")
        found[kind] = found.get(kind, 0) + 1
    return found


def build_row(
    app: FastAPI,
    session_id: str,
    record: ArtifactRecord,
    version: ArtifactVersion,
    *,
    entry: str,
    venue: str = "",
) -> dict[str, Any]:
    """Build ``version`` through its PDF rendition and report the build."""
    from clio_agent.gact.documents import renditions  # noqa: PLC0415
    from clio_agent.gact.documents.latex.rendition import LatexRenditionError  # noqa: PLC0415

    try:
        rendition = renditions.render_pdf(app, session_id, record, version)
    except renditions.RenditionError as exc:
        cause = exc.__cause__
        result = cause.result if isinstance(cause, LatexRenditionError) else None
        diagnostics = [item.as_dict() for item in result.diagnostics] if result else []
        return {
            "status": "failed",
            "entry": entry,
            "source_artifact_id": version.artifact_id,
            "pdf": None,
            "page_count": 0,
            "diagnostics": diagnostics,
            "counts": counts(diagnostics),
            "network": result.network if result else "",
            "error": str(exc),
        }
    producer = rendition.version.producer
    diagnostics = list(producer.get("diagnostics") or [])
    row: dict[str, Any] = {
        "status": "complete" if producer.get("build_ok") else "failed",
        "entry": entry,
        "source_artifact_id": version.artifact_id,
        "source_version": version.version,
        "pdf": {
            "artifact_id": rendition.version.artifact_id,
            "name": rendition.record.name,
            "path": str(rendition.workspace_path or rendition.version.path),
        },
        "page_count": int(producer.get("page_count") or 0),
        "diagnostics": diagnostics,
        "counts": counts(diagnostics),
        "read": sorted(producer.get("source_files") or {}),
        "network": str(producer.get("network") or ""),
    }
    if venue:
        from clio_agent.gact.documents.latex.venue import check_venue  # noqa: PLC0415

        row["venue"] = check_venue(
            venue,
            entry=Path(version.path),
            pdf=Path(row["pdf"]["path"]),
            page_count=row["page_count"],
        )
        if row["venue"].get("status") == "failed":
            row["status"] = "failed"
    if row["status"] == "failed" and "error" not in row:
        failing = [d for d in diagnostics if d.get("kind") not in {"overfull_box", "font_warning"}]
        row["error"] = (
            f"{len(failing)} problem(s) make this build fail" if failing else "venue checks failed"
        )
    return row


def paper_of(app: FastAPI, row: dict[str, Any]) -> PaperMap | None:
    """The map of the build ``row`` reports, or ``None`` when it made no PDF."""
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
    from clio_agent.gact.documents.latex.mapping import paper_map  # noqa: PLC0415

    pdf = row.get("pdf")
    if not pdf:
        return None
    registry = get_registry(app)
    source = registry.get_by_artifact_id(str(row.get("source_artifact_id") or ""))
    rendition = registry.get_by_artifact_id(str(pdf["artifact_id"]))
    if source is None or rendition is None:
        return None
    _record, version = source
    _pdf_record, pdf_version = rendition
    return paper_map(Path(version.path), pdf_version.producer, Path(pdf["path"]))


def outline_row(paper: PaperMap, entry: str) -> dict[str, Any]:
    """The paper's outline as plain data, with counts and unresolved citations."""
    nodes = [node.as_dict() for node in paper.outline]
    counts: dict[str, int] = {}
    unresolved: list[str] = []
    for root in paper.outline:
        for node in root.walk():
            counts[node.kind] = counts.get(node.kind, 0) + 1
            unresolved.extend(c["key"] for c in node.citations if not c["resolved"])
    return {
        "status": "complete",
        "entry": entry,
        "nodes": nodes,
        "counts": counts,
        "unresolved_citations": sorted(set(unresolved)),
        "synctex": paper.index is not None,
    }


def locate_row(
    paper: PaperMap,
    entry: str,
    *,
    page: int = 0,
    x: float = -1,
    y: float = -1,
    file: str = "",
    line: int = 0,
) -> dict[str, Any]:
    """A PDF point's source line, or a source line's places in the PDF."""
    if paper.index is None:
        return {
            "status": "failed",
            "entry": entry,
            "reason": "source_map_unavailable",
            "error": "this build has no SyncTeX index",
        }
    from clio_agent.gact.documents.latex.mapping import describe  # noqa: PLC0415
    from clio_agent.gact.documents.latex.outline import node_at  # noqa: PLC0415

    if file:
        places = paper.index.places(file, line, line)
        node = node_at(paper.outline, file, line)
        return {
            "status": "complete" if places else "failed",
            "entry": entry,
            "direction": "source_to_pdf",
            "file": file,
            "line": line,
            "node": describe(node) if node else "",
            "places": [{"page": p + 1, "rect": [round(v, 1) for v in rect]} for p, rect in places],
            **({} if places else {"error": f"{file} line {line} produces nothing in the PDF"}),
        }
    if page < 1 or x < 0 or y < 0:
        return {
            "status": "failed",
            "entry": entry,
            "reason": "latex_locate_arguments",
            "error": "give page with x and y, or file with line",
        }
    found = paper.index.source_at(page - 1, x, y)
    if found is None:
        return {
            "status": "failed",
            "entry": entry,
            "direction": "pdf_to_source",
            "page": page,
            "error": f"page {page} has no source text",
        }
    source_file, source_line = found
    node = node_at(paper.outline, source_file, source_line)
    return {
        "status": "complete",
        "entry": entry,
        "direction": "pdf_to_source",
        "page": page,
        "x": x,
        "y": y,
        "file": source_file,
        "line": source_line,
        "node": describe(node) if node else "",
    }
