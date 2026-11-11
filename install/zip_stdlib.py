"""Move the bundled standard library's pure-Python modules into ``pythonXY.zip``.

Every file an installation creates is written and, on Windows, scanned by the
antivirus, so the runtime's install time follows its file count (gact-tui#739).
``pythonXY.zip`` is on CPython's default ``sys.path`` on every platform, ahead of the
extracted library, as in the official Windows embeddable distribution. A top-level
module or package moves into it only when it consists of Python source alone, with
precompiled bytecode next to each source. Packages that ship data files (``venv``,
``ensurepip``, ``idlelib``, ...), native extensions, ``site-packages`` and ``os.py``
(the landmark ``getpath`` uses to find the library) stay extracted.

Run with the interpreter being packed, after the bytecode precompile step:

    <python> -I -B install/zip_stdlib.py
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path

KEEP = frozenset({"site-packages", "lib-dynload", "os.py", "__pycache__", "test"})
# Developer notes inside otherwise pure-Python packages; nothing reads them at runtime.
INERT = frozenset(
    {"architecture.rst", "mypy.ini", "README.ctypes", "fetch_macholib", "fetch_macholib.bat"}
)


def _bytecode(source: Path, name: str) -> bytes:
    from importlib._bootstrap_external import _code_to_hash_pyc

    data = source.read_bytes()
    code = compile(data, name, "exec", dont_inherit=True, optimize=0)
    return bytes(_code_to_hash_pyc(code, importlib.util.source_hash(data), checked=False))


def _source_only(tree: Path) -> list[Path] | None:
    files = [item for item in tree.rglob("*") if item.is_file()]
    if not (tree / "__init__.py").is_file():
        return None
    sources = []
    for item in files:
        if "__pycache__" in item.relative_to(tree).parts or item.name in INERT:
            continue
        if item.suffix != ".py":
            return None
        sources.append(item)
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    name = f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    entries = [Path(entry) for entry in sys.path if Path(entry).name == name]
    if not entries:
        raise SystemExit(f"{name} is not on this interpreter's default sys.path")
    archive = entries[0]
    if archive.exists():
        raise SystemExit(f"{archive} already exists")
    moved: list[tuple[Path, list[Path]]] = []
    for item in sorted(stdlib.iterdir()):
        if item.name in KEEP or item.name.startswith("config-"):
            continue
        if item.is_file() and item.suffix == ".py":
            moved.append((item, [item]))
        elif item.is_dir():
            sources = _source_only(item)
            if sources is not None:
                moved.append((item, sources))
    removed = sum(
        sum(1 for entry in top.rglob("*") if entry.is_file()) if top.is_dir() else 1
        for top, _ in moved
    )
    # The precompile step's bytecode for top-level modules lives in stdlib/__pycache__.
    cache = stdlib / "__pycache__"
    stale = (
        [
            entry
            for entry in cache.glob("*.pyc")
            if any(top.is_file() and entry.name.startswith(top.stem + ".") for top, _ in moved)
        ]
        if cache.is_dir()
        else []
    )
    print(
        f"[zip-stdlib] {len(moved)} modules and packages, {removed + len(stale)} files -> {archive}",
        flush=True,
    )
    if args.dry_run:
        return 0
    temporary = archive.with_suffix(".partial")
    # Stored, not deflated: the runtime pack is compressed as a whole, and imports at
    # startup then read bytecode without inflating it.
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as bundle:
        for _, sources in moved:
            for source in sources:
                relative = source.relative_to(stdlib).as_posix()
                bundle.write(source, relative)
                # zipimport reads legacy-layout bytecode next to its source.
                bundle.writestr(relative[:-3] + ".pyc", _bytecode(source, relative))
    temporary.replace(archive)
    for top, _ in moved:
        if top.is_dir():
            shutil.rmtree(top)
        else:
            top.unlink()
    for entry in stale:
        entry.unlink()
    print(f"[zip-stdlib] wrote {archive} ({archive.stat().st_size // 2**20} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
