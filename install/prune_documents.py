"""Remove files the laid-out document stack never reads from a Windows runtime.

Every file in the runtime pack is written and scanned by Windows Defender at install,
about 1,000 files a second (gact-tui#739). What goes, after ``build_document_env.py``
and ``lay_office.py``:

* the document environment's test suites (``tests`` and ``test`` directories, about
  3,400 files, mostly pandas and numpy) and type stubs, which only pytest and type
  checkers read. ``testing`` modules stay: libraries import ``numpy.testing``;
* npm's own documentation, man pages and tests inside ``nodejs_wheel``. npm stays.

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


def _remove(path: Path) -> int:
    if path.is_dir():
        files = sum(1 for item in path.rglob("*") if item.is_file())
        shutil.rmtree(path)
        return files
    if path.is_file():
        path.unlink()
        return 1
    return 0


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
    print(f"[prune-documents] removed {removed} unread files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
