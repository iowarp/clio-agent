"""A built paper's map: its SyncTeX index, sources and outline, from its PDF rendition.

The rendition of a LaTeX version records where its SyncTeX index is and which project files
the build read (``latex.rendition``). This module joins them for the tools that map between the
PDF and the source (``latex_outline``, ``latex_locate``) and for the comment resolver.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.gact.documents.latex.outline import OutlineNode, build_outline, node_at
from clio_agent.gact.documents.latex.synctex import SyncIndex

__all__ = ["PaperMap", "paper_map"]

_CACHE: dict[tuple[str, int, int], PaperMap] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 32


@dataclass
class PaperMap:
    """One build of a paper, ready to map PDF places to source lines and back."""

    entry: str
    root: Path
    texts: dict[str, str]
    outline: list[OutlineNode]
    index: SyncIndex | None
    pdf: Path | None

    def source_line(self, file: str, line: int) -> str:
        """The readable Source line: file, lines and the outline node they fall in."""
        node = node_at(self.outline, file, line)
        where = f"{file}, line {line}"
        return where + (f", in {describe(node)}" if node else "")

    def source_range(self, file: str, start: int, end: int) -> str:
        """The readable Source line for a range of lines."""
        node = node_at(self.outline, file, start)
        where = f"{file}, line {start}" if start == end else f"{file}, lines {start} to {end}"
        return where + (f", in {describe(node)}" if node else "")


def describe(node: OutlineNode) -> str:
    """``Section "Scaling"``, ``figure "Throughput by block size" (fig:tput)`` and the like."""
    kind = {"section": "Section"}.get(node.kind, node.kind)
    title = f' "{node.title}"' if node.title else ""
    label = f" ({node.labels[0]})" if node.labels else ""
    return f"{kind}{title}{label}"


def _read_texts(root: Path, names: list[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for name in names:
        if not name.lower().endswith((".tex", ".ltx", ".bib")):
            continue
        try:
            texts[name] = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts


def paper_map(entry: Path, producer: dict[str, Any], pdf: Path | None = None) -> PaperMap:
    """The map of the build a rendition ``producer`` records, for the paper at ``entry``."""
    root = entry.parent
    names = sorted((producer.get("source_files") or {}).keys()) or [entry.name]
    synctex = Path(str(producer.get("synctex") or "")) if producer.get("synctex") else None
    stamp = 0
    if synctex is not None and synctex.is_file():
        stat = synctex.stat()
        stamp = stat.st_mtime_ns ^ stat.st_size
    key = (str(entry), stamp, hash(tuple(sorted((producer.get("source_files") or {}).items()))))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    texts = _read_texts(root, names)
    index = None
    if synctex is not None and synctex.is_file():
        index = SyncIndex.parse(synctex.read_bytes(), names)
    entry_name = str(producer.get("entry") or entry.name)
    found = PaperMap(entry_name, root, texts, build_outline(entry_name, texts, index), index, pdf)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = found
    return found
