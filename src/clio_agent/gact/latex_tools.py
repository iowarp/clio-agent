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
    from clio_agent.gact.documents import comment_targets  # noqa: PLC0415
    from clio_agent.gact.documents.latex.paper import build_row  # noqa: PLC0415

    app, sid, workspace_id = _context()
    if Path(entry).suffix.lower() not in {".tex", ".ltx"}:
        return {
            "status": "failed",
            "entry": entry,
            "reason": "latex_entry_not_tex",
            "error": f"{entry} is not a .tex file",
        }
    try:
        record, version = comment_targets.register_target(
            app, sid, workspace_id, entry, designation="latex-source"
        )
    except comment_targets.CommentTargetError as exc:
        return {"status": "failed", "entry": entry, "reason": exc.code, "error": str(exc)}
    return build_row(app, sid, record, version, entry=entry, venue=venue)


def _paper(entry: str) -> tuple[Any, dict[str, Any]]:
    """The paper map of ``entry``'s current build, building it when needed."""
    from clio_agent.gact.documents.latex.paper import paper_of  # noqa: PLC0415

    built = latex_build(entry)
    if not built.get("pdf"):
        return None, built
    app, _sid, _workspace_id = _context()
    paper = paper_of(app, built)
    if paper is None:
        return None, {**built, "status": "failed", "error": "the build's records are missing"}
    return paper, built


def latex_outline(entry: str) -> dict[str, Any]:
    """The structure of a LaTeX paper, each part tied to its source lines and PDF pages.

    Returns typed nodes in reading order: section (with level), figure, table, equation,
    algorithm, listing, abstract and bibliography, each with its file, start_line and
    end_line, title or caption, labels, and the pages and rectangles it occupies in the PDF.
    Citations carry their key and whether the .bib defines it. Use it to name "Figure 3" or
    "Section 4.2" and find both its source and its place on the page. It builds the paper
    first when needed.
    """
    from clio_agent.gact.documents.latex.paper import outline_row  # noqa: PLC0415

    paper, built = _paper(entry)
    if paper is None:
        return {**built, "entry": entry, "status": "failed", "nodes": []}
    return outline_row(paper, entry)


def latex_locate(
    entry: str, page: int = 0, x: float = -1, y: float = -1, file: str = "", line: int = 0
) -> dict[str, Any]:
    """Map between a LaTeX paper's PDF and its source.

    Give page (1-based) with x and y (points from the page's top-left corner, as a PDF viewer
    reports them) to get the source file, line and the outline node there. Or give file
    (relative to the entry's folder) and line to get the pages and rectangles that line
    produces in the PDF. It builds the paper first when needed.
    """
    from clio_agent.gact.documents.latex.paper import locate_row  # noqa: PLC0415

    paper, built = _paper(entry)
    if paper is None:
        return {**built, "entry": entry, "status": "failed"}
    return locate_row(paper, entry, page=page, x=x, y=y, file=file, line=line)


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
