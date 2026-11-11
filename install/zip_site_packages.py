"""Move pure-Python distributions of a Windows runtime into one zip on ``sys.path``.

Windows Defender inspects every file an installation creates, so the runtime pack's
cost is mostly its file count. Distributions made only of Python source are imported
from ``site-packages\\clio-pure.zip`` (zipimport, with precompiled bytecode) instead
of tens of thousands of separate files. Anything that could read its own files by
path stays extracted: native code, data files, namespace packages, ``.pth`` hooks and
the explicit ``KEEP`` list.

    <runtime>\\python\\python.exe -I -B install/zip_site_packages.py \\\\
        --site-packages <runtime>\\python\\Lib\\site-packages
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

ARCHIVE = "clio-pure.zip"
SOURCE = {".py", ".pyi"}
# Documentation that ships inside packages but is never read at runtime.
INERT = {".md", ".rst", ".txt", ".keep"}
NATIVE = {".pyd", ".dll", ".exe", ".so", ".lib", ".a", ".dylib"}
# Read their own files by path, patch import machinery, or are imported by tools
# that run outside this interpreter's sys.path.
KEEP = {
    "babel", "botocore", "certifi", "clio_agent", "litellm", "pip", "pkg_resources",
    "setuptools", "_distutils_hack", "pytz", "uv", "wheel",
}


def _records(site: Path):
    for info in sorted(site.glob("*.dist-info")):
        record = info / "RECORD"
        if record.is_file():
            with record.open(encoding="utf-8", newline="") as handle:
                yield info, [row[0] for row in csv.reader(handle) if row]


def _eligible(site: Path, info: Path, files: list[str]) -> list[PurePosixPath] | None:
    """The distribution's files under site-packages, if it can be imported from a zip.

    Native code must be extracted to load. Data files can stay in the zip only when no
    module refers to ``__file__``: ``importlib.resources`` and ``pkgutil.get_data``
    read from a zip, a path built from ``__file__`` does not.
    """
    paths = [PurePosixPath(name) for name in files]
    if any(path.is_absolute() for path in paths):
        return None
    # Console scripts (../../Scripts) stay where they are and import from the zip; the
    # runtime build prunes some recorded files (tests), which are skipped.
    paths = [
        path for path in paths
        if ".." not in path.parts and site.joinpath(*path.parts).is_file()
    ]
    owned = [path for path in paths if not path.parts[0].endswith(".dist-info")]
    if not owned:
        return None
    tops = {path.parts[0] for path in owned}
    if tops & KEEP or any(top.endswith(".pth") for top in tops):
        return None
    data = False
    for path in owned:
        if path.suffix.lower() in NATIVE:
            return None
        if len(path.parts) == 1:
            if path.suffix != ".py":  # a top-level .pth hook or other loose file
                return None
            continue
        if "__pycache__" in path.parts or path.suffix in SOURCE | INERT:
            continue
        if path.name != "py.typed" and "LICENSE" not in path.name.upper():
            data = True
    for top in tops:
        if (site / top).is_dir() and not (site / top / "__init__.py").is_file():
            return None  # PEP 420 portions are shared with other distributions
    if data and any(
        b"__file__" in site.joinpath(*path.parts).read_bytes()
        for path in owned
        if path.suffix == ".py"
    ):
        return None
    return owned


def _bytecode(source: Path, name: str) -> bytes:
    from importlib._bootstrap_external import _code_to_hash_pyc

    data = source.read_bytes()
    code = compile(data, name, "exec", dont_inherit=True, optimize=0)
    return bytes(_code_to_hash_pyc(code, importlib.util.source_hash(data), checked=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    site = args.site_packages.resolve()
    archive = site / ARCHIVE
    if archive.exists():
        raise SystemExit(f"{archive} already exists")
    chosen: list[tuple[Path, list[PurePosixPath]]] = []
    for info, files in _records(site):
        owned = _eligible(site, info, files)
        if owned is not None:
            chosen.append((info, owned))
    tops = {site / path.parts[0] for _, owned in chosen for path in owned}
    moved = sum(
        sum(1 for item in tree.rglob("*") if item.is_file()) if tree.is_dir() else 1
        for tree in tops | {info for info, _ in chosen}
    )
    names = ", ".join(info.name.split("-")[0] for info, _ in chosen)
    print(f"[zip-site] {len(chosen)} distributions, {moved} files: {names}", flush=True)
    if args.dry_run:
        return
    temporary = archive.with_suffix(".partial")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for info, owned in chosen:
            for path in owned:
                if "__pycache__" in path.parts:
                    continue
                source = site.joinpath(*path.parts)
                bundle.write(source, path.as_posix())
                if path.suffix == ".py":
                    # zipimport reads legacy-layout bytecode next to its source.
                    bundle.writestr(
                        path.with_suffix(".pyc").as_posix(), _bytecode(source, path.as_posix())
                    )
            for item in sorted(info.rglob("*")):
                if item.is_file():
                    bundle.write(item, item.relative_to(site).as_posix())
    temporary.rename(archive)
    for info, owned in chosen:
        for top in {path.parts[0] for path in owned}:
            target = site / top
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        shutil.rmtree(info)
    # site.py appends each .pth line to sys.path after site-packages itself.
    (site / "clio-pure.pth").write_text(ARCHIVE + "\n", encoding="utf-8")
    print(f"[zip-site] wrote {archive} ({archive.stat().st_size // 2**20} MB)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
