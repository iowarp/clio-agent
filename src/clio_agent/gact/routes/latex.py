"""The LaTeX workspace's routes: a paper's build, outline and click-through (LaTeX plan).

* ``GET /v1/artifacts/{id}/latex?session_id=`` builds the version (through its PDF rendition,
  the one build the agent's ``latex_build`` uses) and returns the build report, the paper's
  outline, its project folder, the source files the build read with their text (up to 4 MB)
  and each PDF page's size.
* ``GET /v1/artifacts/{id}/latex/locate?session_id=&page=&x=&y=`` gives the source file and
  line at a PDF point; ``&file=&line=`` gives where a source line lands in the PDF.

The viewer stays a thin client: compiling, mapping and venue checks run here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.latex.paper import build_row, locate_row, outline_row, paper_of
from clio_agent.gact.documents.latex.rendition import is_latex

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
    from clio_agent.gact.routes.deps import GactDeps

__all__ = ["register_latex_routes"]

#: The source text one report carries for the source pane (the paper's .tex and .bib files).
_TEXT_BUDGET = 4 * 1024 * 1024


def _page_sizes(pdf: Path) -> list[list[float]]:
    """Each PDF page's width and height in points, for mapping a click to PDF points."""
    from pypdf import PdfReader  # noqa: PLC0415 - heavy import on demand

    try:
        reader = PdfReader(str(pdf))
    except (OSError, ValueError):
        return []
    return [
        [round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2)]
        for page in reader.pages
    ]


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "error": code,
                "message": message,
                "details": {},
                "recoverable": status_code < 500,
            }
        },
    )


def _paper_version(
    app: FastAPI, artifact_id: str, session_id: str
) -> tuple[ArtifactRecord, ArtifactVersion, str]:
    session = app.state.sessions.get(session_id)
    if session is None:
        raise _error(404, "not_found", f"session not found: {session_id}")
    found = get_registry(app).get_by_artifact_id(artifact_id)
    if found is None:
        raise _error(404, "artifact_not_found", f"artifact not found: {artifact_id}")
    record, version = found
    if record.workspace_id != session.workspace_id:
        raise _error(403, "artifact_workspace_mismatch", "artifact is outside this session")
    if not is_latex(record.name):
        raise _error(422, "latex_entry_not_tex", f"{record.name} is not a LaTeX source")
    workspace = app.state.workspaces.get(record.workspace_id)
    root = Path(str(getattr(workspace, "root_path", "") or "")).resolve(strict=False)
    entry = Path(version.path) if version.path else None
    if entry is None:
        raise _error(409, "latex_entry_missing", "the version has no workspace path")
    try:
        relative = entry.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        relative = entry.name
    return record, version, relative


def register_latex_routes(app: FastAPI, deps: GactDeps) -> None:
    """Register the LaTeX workspace's routes."""
    del deps

    def report(artifact_id: str, session_id: str) -> dict[str, Any]:
        record, version, entry = _paper_version(app, artifact_id, session_id)
        row = build_row(app, session_id, record, version, entry=entry)
        paper = paper_of(app, row)
        texts: dict[str, str] = {}
        if paper is not None:
            budget = _TEXT_BUDGET
            for name in sorted(paper.texts):
                text = paper.texts[name]
                if len(text) > budget:
                    break  # the rest are listed in files; the view says it cannot show them
                texts[name] = text
                budget -= len(text)
        return {
            "entry": entry,
            "folder": str(Path(entry).parent.as_posix()) if "/" in entry else "",
            "build": row,
            "outline": outline_row(paper, entry) if paper is not None else None,
            "files": sorted(paper.texts) if paper is not None else [],
            "texts": texts,
            "page_sizes": _page_sizes(paper.pdf) if paper is not None and paper.pdf else [],
        }

    @app.get("/v1/artifacts/{artifact_id}/latex")
    async def latex_report(artifact_id: str, session_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(report, artifact_id, session_id)

    def locate(
        artifact_id: str, session_id: str, page: int, x: float, y: float, file: str, line: int
    ) -> dict[str, Any]:
        record, version, entry = _paper_version(app, artifact_id, session_id)
        row = build_row(app, session_id, record, version, entry=entry)
        paper = paper_of(app, row)
        if paper is None:
            raise _error(409, "latex_build_failed", str(row.get("error") or "no PDF was made"))
        return locate_row(paper, entry, page=page, x=x, y=y, file=file, line=line)

    @app.get("/v1/artifacts/{artifact_id}/latex/locate")
    async def latex_locate(
        artifact_id: str,
        session_id: str,
        page: int = 0,
        x: float = -1,
        y: float = -1,
        file: str = "",
        line: int = 0,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(locate, artifact_id, session_id, page, x, y, file, line)
