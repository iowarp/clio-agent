"""The ``source-map`` resolver: a mark on a LaTeX paper's PDF, named in its source.

Registered for the ``latex`` profile in the comments plan's resolver registry
(``gact/documents/anchors.py``), as the LaTeX plan's addendum A says. A text or region mark on
the compiled PDF resolves like any PDF mark (the text under it, before and after, the crop)
through :class:`~clio_agent.gact.documents.anchor_pdf.PdfResolver`. Its Source line names the
source file, lines and outline node from the build's SyncTeX index. A region on a figure also
names the figure environment and its ``\\includegraphics`` file, with that file's "Produced
by". Re-anchoring after a rebuild moves a text mark by its quote, and the Source line then
comes from the new build's index.

A text mark on a project file in the LaTeX workspace's source pane is a ``text-quote`` that
names its file in ``source_path``. :class:`LatexTextResolver` finds the quote in that file (the
entry when none is named) and adds the outline node to its Source line, so one comment session
on the paper holds marks on every file and on the PDF.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.documents.anchor_pdf import PdfResolver
from clio_agent.gact.documents.anchor_text import TextResolver, find_quote, quote_lines, text_map
from clio_agent.gact.documents.anchors import ResolvedMark, register

if TYPE_CHECKING:
    from clio_agent.gact.documents.anchors import MarkTarget
    from clio_agent.gact.documents.latex.mapping import PaperMap
    from clio_agent.gact.documents.models import DocumentAnchor

__all__ = ["LatexTextResolver", "SourceMapResolver", "register_builtin"]

_GRAPHICS = re.compile(r"\\includegraphics\*?(?:\[[^\]]*\])?\{([^}]+)\}")
_GRAPHIC_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg")


def _page_size(pdf: Path, page: int) -> tuple[float, float] | None:
    from pypdf import PdfReader  # noqa: PLC0415 - heavy import on demand

    try:
        reader = PdfReader(str(pdf))
        box = reader.pages[page].mediabox
        return float(box.width), float(box.height)
    except (OSError, IndexError, ValueError):
        return None


class SourceMapResolver(PdfResolver):
    """PDF marks on a LaTeX paper, with their place in the paper's source."""

    def _paper(self, target: MarkTarget) -> PaperMap | None:
        if "latex_paper" in target.cache:
            return target.cache["latex_paper"]
        from clio_agent.gact.documents.latex.mapping import paper_map  # noqa: PLC0415
        from clio_agent.gact.documents.renditions import find_pdf_rendition  # noqa: PLC0415

        paper = None
        rendition = find_pdf_rendition(target.app, target.record, target.version)
        if rendition is not None and target.version.path:
            paper = paper_map(
                Path(target.version.path), rendition.version.producer, target.view_path
            )
        target.cache["latex_paper"] = paper
        return paper

    def _lines(
        self, target: MarkTarget, page: int | None, rect: Sequence[float] | None
    ) -> tuple[PaperMap | None, dict[str, tuple[int, int]], str | None]:
        if not target.via_rendition:
            return None, {}, None
        paper = self._paper(target)
        if paper is None or paper.index is None:
            return None, {}, "source_map_unavailable: this build has no SyncTeX index"
        if page is None or rect is None:
            return paper, {}, "source_map_unplaced: the mark has no place on a page"
        size = _page_size(target.view_path, page)
        if size is None:
            return paper, {}, "source_map_unavailable: the PDF page could not be read"
        width, height = size
        x, y, w, h = rect
        ranges = paper.index.lines_in(
            page, x * width, y * height, (x + w) * width, (y + h) * height
        )
        if not ranges:
            return paper, {}, "source_map_empty: no source text lies under the mark"
        return paper, ranges, None

    def _source(
        self, target: MarkTarget, page: int | None, rect: Sequence[float] | None, text: str
    ) -> tuple[str | None, str | None]:
        paper, ranges, note = self._lines(target, page, rect)
        if paper is None or not ranges:
            return None, note
        # The file holding most of the lines under the mark is its source; others are named too.
        ordered = sorted(ranges.items(), key=lambda item: item[1][0] - item[1][1])
        parts = [paper.source_range(file, start, end) for file, (start, end) in ordered[:3]]
        return "; ".join(parts), None

    def _region(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        mark = super()._region(target, anchor)
        paper, ranges, _ = self._lines(target, mark.page_index, mark.rect)
        if paper is None or not ranges:
            return mark
        from clio_agent.gact.documents.latex.outline import node_at  # noqa: PLC0415

        seen: set[int] = set()
        for file, (start, end) in ranges.items():
            for line in range(start, end + 1):
                node = node_at(paper.outline, file, line)
                if node is None or node.kind != "figure" or id(node) in seen:
                    continue
                seen.add(id(node))
                mark.lines.extend(self._figure_lines(target, paper, node))
        return mark

    def _figure_lines(
        self, target: MarkTarget, paper: PaperMap, node: object
    ) -> list[tuple[str, str]]:
        from clio_agent.gact.documents.latex.mapping import describe  # noqa: PLC0415

        lines: list[tuple[str, str]] = []
        start, end = node.start_line, node.end_line or node.start_line  # type: ignore[attr-defined]
        file = node.file  # type: ignore[attr-defined]
        lines.append(
            ("Figure", f"{describe(node)}, {file} lines {start} to {end}")  # type: ignore[arg-type]
        )
        body = "\n".join(paper.texts.get(file, "").splitlines()[start - 1 : end])
        for name in _GRAPHICS.findall(body)[:4]:
            lines.extend(self._graphic_lines(target, paper, name.strip()))
        return lines

    def _graphic_lines(
        self, target: MarkTarget, paper: PaperMap, name: str
    ) -> list[tuple[str, str]]:
        from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
        from clio_agent.gact.documents.produced_by import produced_by  # noqa: PLC0415

        candidates = [paper.root / name] + [paper.root / f"{name}{s}" for s in _GRAPHIC_SUFFIXES]
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            return [("Graphics", f"{name}; the file is not in the project")]
        relative = path.relative_to(paper.root).as_posix()
        found = get_registry(target.app).find_version_by_path(
            target.record.workspace_id, str(path.resolve())
        )
        if found is None:
            return [
                ("Graphics", f"{relative}; it is not a registered artifact"),
                ("Made by", "Producer unknown: no recorded tool call made this file."),
            ]
        record, version = found
        made = produced_by(
            target.app, version, workspace_id=target.record.workspace_id, noun="figure"
        )
        lines = [
            (
                "Graphics",
                f"{relative} (artifact {version.artifact_id}, version {version.version})",
            ),
            ("Made by", "\n".join(made.lines).removeprefix("Produced by: ")),
        ]
        if made.known:
            lines.append(("Then", f"rebuild {paper.entry} so the PDF has the new figure."))
        del record
        return lines


class LatexTextResolver(TextResolver):
    """Text quotes on any file of a LaTeX paper's project, with the outline node they fall in."""

    def _project_file(self, target: MarkTarget, anchor: DocumentAnchor) -> tuple[Path, str]:
        entry = Path(target.version.path) if target.version.path else target.path
        name = (anchor.source_path or "").strip().replace("\\", "/")
        if not name or name == entry.name:
            return target.path, entry.name
        root = entry.parent.resolve()
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return target.path, entry.name
        return candidate, candidate.relative_to(root).as_posix()

    def _node(self, target: MarkTarget, file: str, line: int) -> str:
        from clio_agent.gact.documents.latex.mapping import describe  # noqa: PLC0415
        from clio_agent.gact.documents.latex.outline import build_outline, node_at  # noqa: PLC0415

        if "latex_outline" not in target.cache:
            entry = Path(target.version.path) if target.version.path else target.path
            root = entry.parent
            texts: dict[str, str] = {}
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() in {".tex", ".bib"} and path.is_file():
                    relative = path.relative_to(root)
                    if not any(part.startswith(".") for part in relative.parts):
                        texts[relative.as_posix()] = path.read_text(
                            encoding="utf-8", errors="replace"
                        )
            target.cache["latex_outline"] = build_outline(entry.name, texts)
        node = node_at(target.cache["latex_outline"], file, line)
        return describe(node) if node else ""

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        path, name = self._project_file(target, anchor)
        if path == target.path:
            mark = super().resolve(target, anchor)
            file = name
        else:
            mapped = text_map(path.read_text(encoding="utf-8", errors="replace"), "plain")
            found = find_quote(mapped, anchor.exact, anchor.prefix, anchor.suffix, anchor.start)
            if found is None:
                return super().resolve(target, anchor)
            start, end = found
            mark = ResolvedMark(
                mode="text",
                lines=quote_lines(mapped, start, end, source_name=name),
                under=mapped.text[start:end],
            )
            file = name
        lines = []
        for label, value in mark.lines:
            if label == "Source":
                found_line = re.search(r"line (\d+)", value)
                node = self._node(target, file, int(found_line.group(1))) if found_line else ""
                value = f"{value}, in {node}" if node else value
            lines.append((label, value))
        mark.lines = lines
        return mark


def register_builtin() -> None:
    """Text and regions on a LaTeX paper's compiled PDF, and text on its project files."""
    register(("latex",), ("pdf-quad", "region"), SourceMapResolver())
    register(("latex",), ("text-quote",), LatexTextResolver())
