#!/usr/bin/env python3
"""Baseline-0 guard: no bare ``dspy.Tool(`` construction outside the sanctioned modules.

Owner ruling (2026-08-05): "all tools by default need to be instrumented as a
matter of definition." Native tools are constructed through
``clio_agent.gact.agents.tool_instrumentation`` (``native_tool`` /
``boundary_observed_tool`` / ``rebuilt_tool``), which stamps the declared
presentation (representation + curated title) and the observed marker; the
assembly seam (``instrument_tools``) then guarantees every tool notifies the
live tool observer. A bare ``dspy.Tool(...)`` construction anywhere else is a
tool born INVISIBLE — exactly the per-tool-shim regression this program
deleted — so this guard fails on ANY such site (baseline 0).

Sanctioned construction modules (listed explicitly):

* ``src/clio_agent/gact/agents/tool_instrumentation.py`` — the factory itself.
* ``src/clio_agent/tools/execution.py`` — the MCP bridge (``_make_dspy_tool``),
  whose callables notify through the execution boundary and are marked
  observed at construction (``TOOL_OBSERVED_ATTR``).

Detection is AST-based: a ``Call`` whose func is ``<dspy-alias>.Tool`` (from
``import dspy`` / ``import dspy as X``) or a bare ``Tool`` imported from a
``dspy`` module (``from dspy import Tool [as X]``). Tests are not scanned —
they may build bare fixtures to drive the seam.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_tool_instrumentation.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"

# The only modules that may construct dspy.Tool directly (forward-slash paths
# relative to the repository root). This list may only SHRINK.
SANCTIONED_FILES: frozenset[str] = frozenset(
    {
        "src/clio_agent/gact/agents/tool_instrumentation.py",
        "src/clio_agent/tools/execution.py",
        # The ReActV2-internal ``submit`` extract tool (_make_submit_tool): loop
        # MECHANISM, not a model-action tool. It never passes the assembly seam
        # (the loop builds it internally), its wire representation is the typed
        # extract itself (the streamed answer / return contract — a tool row
        # would be a second representation of the same action), and internal
        # loop names are already excluded from tools_called metadata.
        "src/clio_agent/gact/agents/reactv2.py",
    }
)


class Violation(NamedTuple):
    """A bare dspy.Tool construction found outside the sanctioned modules."""

    rel: str
    line: int
    snippet: str


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


class _ToolConstructionVisitor(ast.NodeVisitor):
    """Collect every ``dspy.Tool(...)`` / dspy-imported ``Tool(...)`` call."""

    def __init__(self) -> None:
        self.dspy_aliases: set[str] = set()
        self.tool_aliases: set[str] = set()
        self.sites: list[int] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "dspy" or alias.name.startswith("dspy."):
                self.dspy_aliases.add((alias.asname or alias.name).split(".", 1)[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "dspy" or module.startswith("dspy."):
            for alias in node.names:
                if alias.name == "Tool":
                    self.tool_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "Tool"
            and isinstance(func.value, ast.Name)
            and func.value.id in (self.dspy_aliases or {"dspy"})
        ):
            self.sites.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in self.tool_aliases:
            self.sites.append(node.lineno)
        self.generic_visit(node)


def _construction_sites(source: str, path: str) -> list[int]:
    """Return line numbers of every dspy.Tool construction in ``source``."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:  # pragma: no cover - surfaced as a hard error
        print(f"ERROR: could not parse {path}: {exc}", file=sys.stderr)
        raise
    visitor = _ToolConstructionVisitor()
    visitor.visit(tree)
    return visitor.sites


def check_tool_instrumentation(
    scan_root: Path,
    *,
    rel_to: Path | None = None,
    sanctioned: frozenset[str] | None = None,
) -> list[Violation]:
    """Evaluate the baseline-0 bare-construction guard under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory for the reported forward-slash relative path.
        sanctioned: Override of :data:`SANCTIONED_FILES` (tests).

    Returns:
        Every :class:`Violation` found (empty when the tree is clean).
    """

    base = rel_to if rel_to is not None else scan_root
    allowed = SANCTIONED_FILES if sanctioned is None else sanctioned
    violations: list[Violation] = []
    for path in sorted(scan_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        if rel in allowed:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        for lineno in _construction_sites(source, str(path)):
            snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            violations.append(Violation(rel, lineno, snippet))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 when the tree is clean, 1 on any violation."""

    repo_root = _repo_root()
    violations = check_tool_instrumentation(repo_root / SRC_ROOT, rel_to=repo_root)
    if not violations:
        print(
            f"OK: no bare dspy.Tool construction under {SRC_ROOT} outside the "
            f"sanctioned modules (baseline 0)."
        )
        return 0
    print(f"FAIL: {len(violations)} bare dspy.Tool construction site(s) (baseline 0):")
    for entry in violations:
        print(f"  {entry.rel}:{entry.line}: {entry.snippet}")
    print(
        "Every tool is instrumented by definition: construct native tools via "
        "clio_agent.gact.agents.tool_instrumentation.native_tool (declared title/"
        "representation), boundary-notifying callables via boundary_observed_tool, "
        "and re-wraps via rebuilt_tool. A bare dspy.Tool is a tool born invisible."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
