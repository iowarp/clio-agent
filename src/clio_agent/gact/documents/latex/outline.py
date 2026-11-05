"""A LaTeX paper's outline: typed nodes tied to source lines and PDF places (LaTeX plan).

The outline walks the entry file and the files it ``\\input``s or ``\\include``s, in reading
order, and returns typed nodes:

- ``section`` (any level, with ``level``);
- ``figure``, ``table``, ``equation``, ``algorithm``, ``listing``;
- ``abstract``, ``bibliography``.

Labels and citations attach to the innermost node they fall in. A citation records its key
and whether the paper's ``.bib`` files define it. Each node has its source file and line range
and, with a SyncTeX index, the pages and rectangles it occupies in the PDF. This plays the
role that slide and shape ids play for PowerPoint: "Figure 3" or "Section 4.2" names both its
source and its place on the page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from clio_agent.gact.documents.latex.synctex import SyncIndex

__all__ = ["OutlineNode", "build_outline", "node_at"]

_SECTION_LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
}
_ENVIRONMENTS = {
    "figure": "figure",
    "figure*": "figure",
    "table": "table",
    "table*": "table",
    "equation": "equation",
    "equation*": "equation",
    "align": "equation",
    "align*": "equation",
    "gather": "equation",
    "gather*": "equation",
    "multline": "equation",
    "multline*": "equation",
    "eqnarray": "equation",
    "algorithm": "algorithm",
    "algorithm*": "algorithm",
    "lstlisting": "listing",
    "listing": "listing",
    "abstract": "abstract",
    "thebibliography": "bibliography",
}
_TOKEN = re.compile(
    r"\\(?P<section>part|chapter|section|subsection|subsubsection|paragraph)\*?"
    r"(?:\[[^\]]*\])?\{"
    r"|\\begin\{(?P<begin>[a-zA-Z*]+)\}"
    r"|\\end\{(?P<end>[a-zA-Z*]+)\}"
    r"|\\(?:input|include)\{(?P<input>[^}]+)\}"
    r"|\\label\{(?P<label>[^}]+)\}"
    r"|\\caption(?:\[[^\]]*\])?\{"
    r"|\\(?:cite[a-zA-Z]*|nocite)\*?(?:\[[^\]]*\])*\{(?P<cite>[^}]+)\}"
    r"|\\(?:bibliography\{(?P<bibfiles>[^}]+)\}|printbibliography)"
    r"|\\addbibresource\{(?P<resource>[^}]+)\}"
)
_BIB_KEY = re.compile(r"@\s*[a-zA-Z]+\s*\{\s*([^,\s]+)\s*,")


@dataclass
class OutlineNode:
    """One typed part of the paper."""

    kind: str
    file: str
    start_line: int
    end_line: int = 0
    title: str = ""
    level: int = 0
    labels: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    places: list[dict[str, Any]] = field(default_factory=list)
    children: list[OutlineNode] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The node and its children as plain data."""
        row: dict[str, Any] = {
            "kind": self.kind,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line or self.start_line,
        }
        for key in ("title", "labels", "citations", "places"):
            value = getattr(self, key)
            if value:
                row[key] = value
        if self.kind == "section":
            row["level"] = self.level
        if self.children:
            row["children"] = [child.as_dict() for child in self.children]
        return row

    def walk(self) -> list[OutlineNode]:
        """This node and every node under it, in order."""
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


def _strip(line: str) -> str:
    return re.sub(r"(?<!\\)%.*$", "", line)


def _braced(text: str, start: int) -> str:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return text[start + 1 :]


def _title(text: str) -> str:
    clean = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    return " ".join(clean.replace("{", " ").replace("}", " ").replace("~", " ").split())[:200]


def _resolve_input(name: str, current: str, texts: Mapping[str, str]) -> str:
    base = PurePosixPath(current).parent
    for candidate in (name, f"{name}.tex"):
        for path in (PurePosixPath(candidate), base / candidate):
            clean = path.as_posix().removeprefix("./")
            if clean in texts:
                return clean
    return ""


def _reading_order(entry: str, texts: Mapping[str, str]) -> list[tuple[str, int, str]]:
    """``(file, line, text)`` of every source line in reading order, following inputs."""
    lines: list[tuple[str, int, str]] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen or name not in texts:
            return
        seen.add(name)
        for number, raw in enumerate(texts[name].splitlines(), start=1):
            code = _strip(raw)
            lines.append((name, number, code))
            for match in re.finditer(r"\\(?:input|include)\{([^}]+)\}", code):
                target = _resolve_input(match.group(1).strip(), name, texts)
                if target:
                    visit(target)

    visit(entry)
    return lines


def _bib_keys(files: list[str], texts: Mapping[str, str]) -> set[str]:
    keys: set[str] = set()
    for name in files:
        stem = name.strip()
        for candidate in (stem, f"{stem}.bib"):
            if candidate in texts:
                keys.update(_BIB_KEY.findall(texts[candidate]))
    return keys


def build_outline(
    entry: str, texts: Mapping[str, str], index: SyncIndex | None = None
) -> list[OutlineNode]:
    """The outline of the paper whose entry is ``entry``, from its sources by path."""
    order = _reading_order(entry, texts)
    roots: list[OutlineNode] = []
    sections: list[OutlineNode] = []  # the open section at each depth
    open_envs: list[tuple[str, OutlineNode | None]] = []
    nodes: list[OutlineNode] = []
    citations: list[tuple[OutlineNode | None, str]] = []
    bib_files: list[str] = []
    last_line: dict[str, int] = {}

    def close(node: OutlineNode, file: str, number: int) -> None:
        # A section ends just before what closes it, or, when that is in another file, at the
        # last line read from its own file.
        if not node.end_line:
            end = number - 1 if node.file == file else last_line.get(node.file, node.start_line)
            node.end_line = max(end, node.start_line)

    def innermost() -> OutlineNode | None:
        for _, node in reversed(open_envs):
            if node is not None:
                return node
        return sections[-1] if sections else None

    def attach(node: OutlineNode) -> None:
        parent = innermost()
        (parent.children if parent else roots).append(node)
        nodes.append(node)

    for file, number, code in order:
        last_line[file] = number
        for match in _TOKEN.finditer(code):
            if match.group("section"):
                kind = match.group("section")
                level = _SECTION_LEVELS[kind]
                title = _title(_braced(code, match.end() - 1))
                while sections and sections[-1].level >= level:
                    closed = sections.pop()
                    close(closed, file, number)
                node = OutlineNode("section", file, number, title=title, level=level)
                parent = sections[-1] if sections else None
                (parent.children if parent else roots).append(node)
                nodes.append(node)
                sections.append(node)
            elif match.group("begin"):
                env = match.group("begin")
                if env in _ENVIRONMENTS:
                    node = OutlineNode(_ENVIRONMENTS[env], file, number)
                    attach(node)
                    open_envs.append((env, node))
                else:
                    open_envs.append((env, None))
            elif match.group("end"):
                env = match.group("end")
                for index_ in range(len(open_envs) - 1, -1, -1):
                    if open_envs[index_][0] == env:
                        _, node = open_envs.pop(index_)
                        if node is not None:
                            node.end_line = number
                        break
            elif match.group("label"):
                target = innermost()
                if target is not None:
                    target.labels.append(match.group("label").strip())
            elif match.group(0).startswith("\\caption"):
                target = innermost()
                if target is not None and not target.title:
                    target.title = _title(_braced(code, match.end() - 1))
            elif match.group("cite"):
                for key in match.group("cite").split(","):
                    if key.strip():
                        citations.append((innermost(), key.strip()))
            elif match.group("bibfiles") is not None or match.group(0) == "\\printbibliography":
                if match.group("bibfiles"):
                    bib_files.extend(match.group("bibfiles").split(","))
                # The bibliography is its own part of the paper, after every section.
                while sections:
                    closed = sections.pop()
                    close(closed, file, number)
                node = OutlineNode(
                    "bibliography", file, number, end_line=number, title="References"
                )
                roots.append(node)
                nodes.append(node)
            elif match.group("resource"):
                bib_files.append(match.group("resource"))
    for node in sections:
        node.end_line = node.end_line or last_line.get(node.file, node.start_line)
    keys = _bib_keys(bib_files, texts)
    for owner, key in citations:
        if owner is not None:
            owner.citations.append({"key": key, "resolved": key in keys})
    if index is not None:
        for node in nodes:
            node.places = [
                {"page": page + 1, "rect": [round(value, 1) for value in rect]}
                for page, rect in index.places(
                    node.file, node.start_line, node.end_line or node.start_line
                )
            ]
    return roots


def node_at(roots: list[OutlineNode], file: str, line: int) -> OutlineNode | None:
    """The innermost node containing ``file`` line ``line``."""
    best: OutlineNode | None = None
    for root in roots:
        for node in root.walk():
            end = node.end_line or node.start_line
            if node.file == file and node.start_line <= line <= end:
                if best is None or (end - node.start_line) <= (
                    (best.end_line or best.start_line) - best.start_line
                ):
                    best = node
    return best
