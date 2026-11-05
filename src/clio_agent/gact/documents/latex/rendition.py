"""The PDF rendition of a LaTeX source, through the one build (LaTeX plan, "Projects").

A LaTeX version renders by building its project from the workspace folder the version lives
in, not from the single file's stored bytes, since ``\\input`` files, the bibliography and the
figures are separate files. The entry's bytes must still be the version's (else the version
changed under the build). The rendition records the hash of every project file the build read
(``source_files``), and is reused only while each still matches, so editing a section or the
``.bib`` rebuilds the paper (L6). The SyncTeX index is kept beside the PDF preview.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.documents.latex.build import BuildResult, LatexBuildError, build_project
from clio_agent.gact.documents.latex.project import ProjectError

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.records import ArtifactVersion

__all__ = ["LatexRenditionError", "build_rendition", "is_latex", "sources_current"]


class LatexRenditionError(RuntimeError):
    """The LaTeX version could not be built to a PDF; the message names why.

    ``result`` is the build when it ran, so a caller can show its diagnostics.
    """

    def __init__(self, message: str, result: BuildResult | None = None) -> None:
        super().__init__(message)
        self.result = result


def is_latex(name: str) -> bool:
    """Whether an artifact named ``name`` renders through the LaTeX build."""
    return Path(name).suffix.lower() in {".tex", ".ltx"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(version: ArtifactVersion) -> Path:
    if not version.path:
        raise LatexRenditionError("the LaTeX version has no workspace path to build from")
    entry = Path(version.path)
    if not entry.is_file():
        raise LatexRenditionError(f"{entry.name} is no longer in the workspace")
    if version.sha256 and _sha256(entry) != version.sha256:
        raise LatexRenditionError(
            f"{entry.name} changed since this version; render the newest version instead"
        )
    return entry


def sources_current(version: ArtifactVersion, producer: dict[str, Any]) -> bool:
    """Whether every project file a retained LaTeX rendition read still has its hash."""
    files = producer.get("source_files")
    if not isinstance(files, dict) or not version.path:
        return True  # not a project rendition: the source version's own hash binds it
    root = Path(version.path).parent
    for name, digest in files.items():
        path = root / name
        try:
            if not path.is_file() or _sha256(path) != digest:
                return False
        except OSError:
            return False
    return True


def _failure(result: BuildResult) -> str:
    failing = [item for item in result.diagnostics if item.kind != "font_warning"][:5]
    parts = [
        f"{item.kind}: {item.message}" + (f" ({item.file}:{item.line})" if item.file else "")
        for item in failing
    ]
    return "LaTeX build produced no PDF: " + ("; ".join(parts) or "no diagnosis")


def build_rendition(
    version: ArtifactVersion, *, workspace_root: Path, target: Path
) -> tuple[str, dict[str, Any]]:
    """Build the version's project, write its PDF to ``target`` and its SyncTeX beside it.

    Returns the converter name and the producer fields the rendition records.

    Raises:
        LatexRenditionError: the entry changed or is gone, the project cannot be staged, the
            build could not run, or it produced no PDF.
    """
    entry = _entry(version)
    try:
        result = build_project(entry, workspace_root=workspace_root)
    except (ProjectError, LatexBuildError) as exc:
        raise LatexRenditionError(f"{exc.reason}: {exc}") from exc
    if not result.pdf:
        raise LatexRenditionError(_failure(result), result)
    temporary = target.with_name(f".{target.name}.build")
    temporary.write_bytes(result.pdf)
    temporary.replace(target)
    if result.synctex:
        target.with_suffix(".synctex.gz").write_bytes(result.synctex)
    return "tectonic", {
        "entry": result.entry,
        "source_files": dict(sorted(result.read.items())),
        "build_ok": result.ok,
        "diagnostics": [item.as_dict() for item in result.diagnostics][:200],
        "page_count": result.page_count,
        "network": result.network,
    }
