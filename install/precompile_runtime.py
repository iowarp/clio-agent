"""Precompile the Python modules used by the bundled GACT startup path."""

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


def precompile_startup_modules(python_root: Path) -> int:
    """Build the app and write portable bytecode for its loaded modules."""

    root = python_root.resolve(strict=True)
    sys.dont_write_bytecode = True

    from clio_agent.gact.app import build_app

    build_app()
    sources = _loaded_runtime_sources(root)
    if not sources:
        raise RuntimeError(f"GACT startup loaded no Python sources under {root}")

    for source in sources:
        cache_path = Path(importlib.util.cache_from_source(str(source)))
        display_path = Path("gact-runtime/python") / source.relative_to(root)
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
    return len(sources)


def main() -> int:
    """Run the bundled-runtime startup precompiler."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--python-root", required=True, type=Path)
    args = parser.parse_args()
    compiled = precompile_startup_modules(args.python_root)
    print(f"prepared {compiled} startup bytecode files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
