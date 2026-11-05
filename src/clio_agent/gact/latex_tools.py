"""The ``latex`` plugin's built-in tools (LaTeX plan, "Tools"): build, outline and locate.

``latex_build`` builds a paper through the one build (the PDF rendition of its entry file), so
the agent and the person's preview share one PDF, one cache and one provenance record. It
returns the PDF artifact, page count, typed diagnostics and, with a venue, the venue checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["build_latex_build_tool"]


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
