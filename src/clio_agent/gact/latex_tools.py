"""The ``latex`` plugin's built-in tools (LaTeX plan, "Tools"): build, outline and locate.

``latex_build`` builds a paper through the one build (the PDF rendition of its entry file), so
the agent and the person's preview share one PDF, one cache and one provenance record. It
returns the PDF artifact, page count, typed diagnostics and, with a venue, the venue checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["build_latex_build_tool", "build_latex_locate_tool", "build_latex_outline_tool"]


def _context() -> tuple[Any, str, str]:
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

    app, sid = _ctx.active_app(), _ctx.active_session_id()
    if app is None or not sid:
        raise ValueError("latex tools need an active session")
    workspace_id = _session_workspace_id(app, sid)
    if not workspace_id:
        raise ValueError("latex tools need a session in a workspace")
    return app, sid, workspace_id


def _counts(diagnostics: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in diagnostics:
        kind = str(item.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def latex_build(entry: str, venue: str = "") -> dict[str, Any]:
    """Build a LaTeX paper to PDF and report its problems.

    entry is the paper's main .tex file (the one with \\documentclass), as a workspace path;
    its folder is the project, so \\input files, the .bib and figures are found beside it.
    The result names the PDF artifact, the page count and every problem by kind, file and line:
    error, undefined_reference, undefined_citation, missing_file, missing_package,
    overfull_box, font_warning, shell_escape_unsupported, unsupported_bibliography_backend.
    status is failed when the paper has errors or broken references, even if a PDF was made.
    venue (a template name such as ieee-conference or acm-sigconf) adds that venue's checks:
    page limit, double-blind and required packages. Report missing_package and shell escape
    messages to the person as written; do not retry them.
    """
    from clio_agent.gact.documents.comment_targets import (  # noqa: PLC0415
        CommentTargetError,
        register_target,
    )
    from clio_agent.gact.documents.latex.rendition import LatexRenditionError  # noqa: PLC0415
    from clio_agent.gact.documents.renditions import RenditionError, render_pdf  # noqa: PLC0415

    app, sid, workspace_id = _context()
    if Path(entry).suffix.lower() not in {".tex", ".ltx"}:
        return {
            "status": "failed",
            "entry": entry,
            "reason": "latex_entry_not_tex",
            "error": f"{entry} is not a .tex file",
        }
    try:
        record, version = register_target(app, sid, workspace_id, entry, designation="latex-source")
    except CommentTargetError as exc:
        return {"status": "failed", "entry": entry, "reason": exc.code, "error": str(exc)}
    try:
        rendition = render_pdf(app, sid, record, version)
    except RenditionError as exc:
        cause = exc.__cause__
        result = cause.result if isinstance(cause, LatexRenditionError) else None
        diagnostics = [item.as_dict() for item in result.diagnostics] if result else []
        return {
            "status": "failed",
            "entry": entry,
            "pdf": None,
            "page_count": 0,
            "diagnostics": diagnostics,
            "counts": _counts(diagnostics),
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
        "counts": _counts(diagnostics),
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


def _paper(entry: str) -> tuple[Any, dict[str, Any] | None]:
    """The paper map of ``entry``'s current build, building it when needed."""
    from clio_agent.gact.documents.latex.mapping import paper_map  # noqa: PLC0415

    built = latex_build(entry)
    pdf = built.get("pdf")
    if not pdf:
        return None, built
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    app, _sid, _workspace_id = _context()
    registry = get_registry(app)
    source = registry.get_by_artifact_id(str(built.get("source_artifact_id") or ""))
    rendition = registry.get_by_artifact_id(str(pdf["artifact_id"]))
    if source is None or rendition is None:
        return None, {**built, "status": "failed", "error": "the build's records are missing"}
    _record, version = source
    _pdf_record, pdf_version = rendition
    return paper_map(Path(version.path), pdf_version.producer, Path(pdf["path"])), built


def latex_outline(entry: str) -> dict[str, Any]:
    """The structure of a LaTeX paper, each part tied to its source lines and PDF pages.

    Returns typed nodes in reading order: section (with level), figure, table, equation,
    algorithm, listing, abstract and bibliography, each with its file, start_line and
    end_line, title or caption, labels, and the pages and rectangles it occupies in the PDF.
    Citations carry their key and whether the .bib defines it. Use it to name "Figure 3" or
    "Section 4.2" and find both its source and its place on the page. It builds the paper
    first when needed.
    """
    paper, built = _paper(entry)
    if paper is None:
        return {**(built or {}), "entry": entry, "status": "failed", "nodes": []}
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


def latex_locate(
    entry: str, page: int = 0, x: float = -1, y: float = -1, file: str = "", line: int = 0
) -> dict[str, Any]:
    """Map between a LaTeX paper's PDF and its source.

    Give page (1-based) with x and y (points from the page's top-left corner, as a PDF viewer
    reports them) to get the source file, line and the outline node there. Or give file
    (relative to the entry's folder) and line to get the pages and rectangles that line
    produces in the PDF. It builds the paper first when needed.
    """
    paper, built = _paper(entry)
    if paper is None:
        return {**(built or {}), "entry": entry, "status": "failed"}
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


def build_latex_outline_tool() -> Any:
    """The ``latex_outline`` native tool."""
    from clio_agent.gact.agents.native_presenters_latex import (  # noqa: PLC0415
        latex_outline_presentation,
    )
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(
        latex_outline,
        name="latex_outline",
        domain="workspace",
        title="Outline LaTeX paper",
        desc=latex_outline.__doc__,
        args={"entry": {"type": "string", "description": "Workspace path of the main .tex file."}},
        presentation=latex_outline_presentation,
    )


def build_latex_locate_tool() -> Any:
    """The ``latex_locate`` native tool."""
    from clio_agent.gact.agents.native_presenters_latex import (  # noqa: PLC0415
        latex_locate_presentation,
    )
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(
        latex_locate,
        name="latex_locate",
        domain="workspace",
        title="Locate in LaTeX paper",
        desc=latex_locate.__doc__,
        args={
            "entry": {"type": "string", "description": "Workspace path of the main .tex file."},
            "page": {"type": "integer", "default": 0, "description": "PDF page, from 1."},
            "x": {"type": "number", "default": -1, "description": "Points from the left."},
            "y": {"type": "number", "default": -1, "description": "Points from the top."},
            "file": {"type": "string", "default": "", "description": "Source file."},
            "line": {"type": "integer", "default": 0, "description": "Source line."},
        },
        presentation=latex_locate_presentation,
    )


def build_latex_build_tool() -> Any:
    """The ``latex_build`` native tool with its readable build presentation."""
    from clio_agent.gact.agents.native_presenters_latex import (  # noqa: PLC0415
        latex_build_presentation,
        latex_build_start,
    )
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(
        latex_build,
        name="latex_build",
        domain="workspace",
        title="Build LaTeX paper",
        desc=latex_build.__doc__,
        args={
            "entry": {
                "type": "string",
                "description": "Workspace path of the paper's main .tex file.",
            },
            "venue": {
                "type": "string",
                "default": "",
                "description": "Optional template name whose venue rules to check.",
            },
        },
        presentation=latex_build_presentation,
        presentation_start=latex_build_start,
    )
