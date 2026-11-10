"""Precompile the Python modules used by the bundled GACT startup path.

``--package NAME`` also compiles every module of a first-party package. The server
runs with ``-B`` from a signed, read-only bundle, so a module imported lazily after
startup (agent tools, providers, the document runtime) would otherwise be compiled
in memory on every launch. Third-party packages stay startup-only: compiling all of
them adds well over 100 MB to the bundle.
"""

from __future__ import annotations

import argparse
import importlib.util
import py_compile
import sys
from pathlib import Path


def _loaded_runtime_sources(python_root: Path) -> list[Path]:
    """Return loaded Python sources contained by ``python_root``."""

    sources: set[Path] = set()
    for module in tuple(sys.modules.values()):
        value = getattr(module, "__file__", None)
        if not isinstance(value, str):
            continue
        source = Path(value)
        if source.suffix not in {".py", ".pyw"}:
            continue
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(python_root)
        except (FileNotFoundError, ValueError):
            continue
        sources.add(resolved)
    return sorted(sources)


def precompile_startup_modules(python_root: Path, source_root: Path | None = None) -> int:
    """Build the app and write portable bytecode for its loaded modules."""

    root = python_root.resolve(strict=True)
    sys.dont_write_bytecode = True

    from clio_agent.gact.app import build_app

    build_app()
    sources = _loaded_runtime_sources(root)
    if source_root is not None:
        sources = sorted(
            set(sources) | set(_loaded_runtime_sources(source_root.resolve(strict=True)))
        )
    if not sources:
        raise RuntimeError(f"GACT startup loaded no Python sources under {root}")

    for source in sources:
        _compile(source, root if source_root is None else None)
    return len(sources)


def precompile_packages(python_root: Path, names: list[str]) -> int:
    """Write portable bytecode for every module of the named packages under ``python_root``."""

    root = python_root.resolve(strict=True)
    sources: set[Path] = set()
    for name in names:
        spec = importlib.util.find_spec(name)
        locations = list(spec.submodule_search_locations or []) if spec else []
        if not locations:
            raise RuntimeError(f"{name} is not an importable package")
        for location in locations:
            package = Path(location).resolve(strict=True)
            if not package.is_relative_to(root):
                # An editable source install is prepared through --source-root.
                continue
            sources.update(path for path in package.rglob("*.py") if path.is_file())
    for source in sorted(sources):
        _compile(source, root)
    return len(sources)


def _compile(source: Path, root: Path | None) -> None:
    """Compile ``source`` beside itself, naming it relative to the bundled runtime."""

    cache_path = Path(importlib.util.cache_from_source(str(source)))
    display_path = (
        Path("gact-runtime/python") / source.relative_to(root) if root is not None else source
    )
    py_compile.compile(
        str(source),
        cfile=str(cache_path),
        dfile=display_path.as_posix(),
        doraise=True,
        # CHECKED_HASH, never UNCHECKED_HASH: the desktop upgrades this
        # runtime in place with ``uv pip install``, which rewrites sources
        # but cannot remove bytecode it did not write. Unchecked bytecode
        # then keeps executing the previous release's dependencies
        # (0.9.4.14 -> 0.9.4.15 stale clio_schemas ImportError). A source
        # hash is portable across relocation like the unchecked form, and
        # Python recompiles any module whose source changed.
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )


def main() -> int:
    """Run the bundled-runtime startup precompiler."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--python-root", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, help="also prepare an editable source install")
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="also compile every module of this package (repeatable)",
    )
    args = parser.parse_args()
    compiled = precompile_startup_modules(args.python_root, args.source_root)
    print(f"prepared {compiled} startup bytecode files")
    if args.package:
        packaged = precompile_packages(args.python_root, args.package)
        print(f"prepared {packaged} package bytecode files ({', '.join(args.package)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
