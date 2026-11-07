"""A LaTeX project: its entry file and the source files a build stages (LaTeX plan, "Projects").

A build takes an entry ``.tex`` and the folder it belongs to. When the caller names no entry,
the folder's one file with ``\\documentclass`` is the entry. The folder's source files are
copied into the build directory, bounded by count and size, and each is hashed so a rendition
can be bound to the files the build read.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MAX_FILES",
    "MAX_TOTAL_BYTES",
    "ProjectError",
    "StagedProject",
    "find_entry",
    "stage_project",
]

#: Bounds on what one build copies (a paper's sources, figures and data).
MAX_FILES = 2000
MAX_TOTAL_BYTES = 256 * 1024 * 1024

# What a paper reads: TeX sources, bibliography and style files, figures and the data files a
# figure or table reads through \input or pgfplots.
_SOURCE_SUFFIXES = frozenset(
    {
        ".tex",
        ".ltx",
        ".bib",
        ".bst",
        ".bbx",
        ".cbx",
        ".cls",
        ".sty",
        ".clo",
        ".cfg",
        ".def",
        ".png",
        ".jpg",
        ".jpeg",
        ".pdf",
        ".eps",
        ".csv",
        ".dat",
        ".txt",
        ".tsv",
        ".tikz",
        ".pgf",
    }
)
# Folders that are never a paper's sources.
_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "artifacts", ".clio"}
)
_DOCUMENTCLASS = re.compile(r"^[^%]*\\documentclass\b", re.MULTILINE)
_HEAD_BYTES = 64 * 1024


class ProjectError(ValueError):
    """The project cannot be built as given; ``reason`` is the typed refusal."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class StagedProject:
    """The sources copied into a build directory, with their hashes."""

    root: Path
    entry: str
    sources: dict[str, str] = field(default_factory=dict)

    def texts(self) -> dict[str, str]:
        """The staged ``.tex`` and ``.bib`` texts by project-relative path."""
        out: dict[str, str] = {}
        for name in self.sources:
            if name.lower().endswith((".tex", ".ltx", ".bib")):
                try:
                    out[name] = (self.root / name).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        return out


def _is_entry(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return False
    return bool(_DOCUMENTCLASS.search(head))


def find_entry(folder: Path) -> Path:
    """The folder's one ``.tex`` file with ``\\documentclass``.

    Raises:
        ProjectError: ``latex_entry_missing`` when there is none, ``latex_entry_ambiguous`` when
            there are several (the caller names one).
    """
    candidates = sorted(path for path in folder.glob("*.tex") if path.is_file() and _is_entry(path))
    if not candidates:
        raise ProjectError(
            "latex_entry_missing", f"{folder.name} has no .tex file with \\documentclass"
        )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ProjectError(
            "latex_entry_ambiguous", f"{folder.name} has several entry files ({names}); name one"
        )
    return candidates[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_project(entry: Path, destination: Path) -> StagedProject:
    """Copy the entry's folder's source files into ``destination`` and hash them.

    The entry's folder is the project root. Files are copied with their relative paths, so
    ``\\input{sections/eval}`` and ``\\includegraphics{figs/a.png}`` resolve as in the workspace.

    Raises:
        ProjectError: ``latex_entry_missing`` when the entry is not a file, or
            ``latex_project_too_large`` past :data:`MAX_FILES` or :data:`MAX_TOTAL_BYTES`.
    """
    entry = entry.resolve()
    if not entry.is_file():
        raise ProjectError("latex_entry_missing", f"{entry.name} is not a file")
    root = entry.parent
    staged = StagedProject(root=destination, entry=entry.name)
    total = 0
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(
            name for name in folders if name not in _SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(files):
            source = Path(directory) / name
            if source.suffix.lower() not in _SOURCE_SUFFIXES or source.is_symlink():
                continue
            relative = source.relative_to(root).as_posix()
            size = source.stat().st_size
            total += size
            if len(staged.sources) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ProjectError(
                    "latex_project_too_large",
                    f"{root.name} has more than {MAX_FILES} source files or "
                    f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB",
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            staged.sources[relative] = _sha256(target)
    if entry.name not in staged.sources:
        raise ProjectError("latex_entry_missing", f"{entry.name} is not a LaTeX source")
    return staged
