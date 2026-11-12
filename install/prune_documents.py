"""Remove files the laid-out document stack never reads from a Windows runtime.

Every file in the runtime pack is written and scanned by Windows Defender at install,
about 1,000 files a second (gact-tui#739). What goes, after ``build_document_env.py``
and ``lay_office.py``:

* the document environment's test suites (``tests`` and ``test`` directories, about
  3,400 files, mostly pandas and numpy) and type stubs, which only pytest and type
  checkers read. ``testing`` modules stay: libraries import ``numpy.testing``;
* npm's own documentation, man pages and tests inside ``nodejs_wheel``, and corepack,
  which nothing runs. npm itself stays: the Obsidian client installs with ``npm ci``
  (node-gyp included) and pnpm falls back to npm;
* C build inputs: headers, Cython sources and import libraries that only a compiler
  reads (``pymupdf/mupdf-devel``, lxml and numpy includes). cffi keeps its headers,
  which it reads when it compiles at run time;
* Node packages' Markdown and TypeScript declarations, which Node never loads.
  Licences stay.

``lay_office.py`` already pruned LibreOffice with ``office.prune_unread``, the list
shared with macOS and Linux.

Usage: ``prune_documents.py --runtime <runtime>``
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

TEST_DIRECTORIES = frozenset({"tests", "test"})
NPM_UNREAD = ("docs", "man", "test")
BUILD_ONLY_SUFFIXES = frozenset({".h", ".hpp", ".c", ".cpp", ".pxd", ".pyx"})
BUILD_ONLY_DIRECTORIES = ("pymupdf/mupdf-devel", "numpy/_core/include")
RUNTIME_COMPILERS = frozenset({"cffi"})
NODE_UNREAD = (".md", ".markdown", ".d.ts", ".d.mts", ".d.cts", ".map")


def _remove(path: Path) -> int:
    if path.is_dir():
        files = sum(1 for item in path.rglob("*") if item.is_file())
        shutil.rmtree(path)
        return files
    if path.is_file():
        path.unlink()
        return 1
    return 0


def prune_build_inputs(site: Path) -> int:
    removed = sum(_remove(site / name) for name in BUILD_ONLY_DIRECTORIES)
    for path in list(site.rglob("*")):
        if (
            path.suffix.lower() in BUILD_ONLY_SUFFIXES
            and path.relative_to(site).parts[0] not in RUNTIME_COMPILERS
            and path.is_file()
        ):
            removed += _remove(path)
    return removed


def prune_node_packages(node_modules: Path) -> int:
    removed = 0
    for path in list(node_modules.rglob("*")):
        name = path.name.lower()
        if name.endswith(NODE_UNREAD) and not name.startswith(("license", "licence")):
            removed += _remove(path) if path.is_file() else 0
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    documents = args.runtime.resolve() / "documents"
    site = documents / "python" / "Lib" / "site-packages"
    if not site.is_dir():
        raise SystemExit(f"{site} is missing; run build_document_env.py first")
    removed = 0
    # Top-down, so a removed test tree is not walked again.
    for directory in sorted(
        (path for path in site.rglob("*") if path.is_dir() and path.name in TEST_DIRECTORIES),
        key=lambda path: len(path.parts),
    ):
        if directory.exists():
            removed += _remove(directory)
    for stub in site.rglob("*.pyi"):
        removed += _remove(stub)
    npm = site / "nodejs_wheel" / "lib" / "node_modules" / "npm"
    for name in NPM_UNREAD:
        removed += _remove(npm / name)
    removed += _remove(npm.parent / "corepack")
    removed += prune_build_inputs(site)
    removed += prune_node_packages(npm.parent)
    if (documents / "node_modules").is_dir():
        removed += prune_node_packages(documents / "node_modules")
    print(f"[prune-documents] removed {removed} unread files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
