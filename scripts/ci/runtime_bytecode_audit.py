#!/usr/bin/env python3
"""Measure an installed desktop runtime's bytecode coverage, bytecode drift and log size.

#1979 found on macOS that modules the build did not precompile get their ``.pyc``
written into the installed runtime at prepare time and on later launches. Where the
runtime is read-only (Linux ``/usr/lib``) those writes fail and the module is
recompiled on every launch; where it is writable (Windows ``%LOCALAPPDATA%``) the
files drift from what was shipped. #1983 caps ``clio-runtime.log``. This script
records all three on a real install, so the Linux and Windows smoke jobs carry
evidence instead of assumptions.

    python runtime_bytecode_audit.py snapshot --runtime DIR --out before.json
    (launch the desktop)
    python runtime_bytecode_audit.py report --runtime DIR --snapshot before.json \
        --log-root AGENT_HOME --out report.json --summary "$GITHUB_STEP_SUMMARY"

Stdlib only: it runs before and beside the runtime it inspects. Coverage and drift
are report-only; ``report`` exits 1 only when a ``clio-runtime.log`` file exceeds
``--fail-log-bytes``, because that is the bounded-log contract #1983 promises.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

FIRST_PARTY = ("clio_agent", "clio_kit", "clio_schemas")
#: Lazily imported stdlib modules #1979 saw written into the macOS bundle.
WATCHED_STDLIB = (
    "xml/dom/minidom.py",
    "xml/sax/__init__.py",
    "xml/parsers/expat.py",
    "plistlib.py",
    "statistics.py",
    "timeit.py",
    "_strptime.py",
    "encodings/cp1252.py",
    "encodings/mac_roman.py",
)
NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")
DEFAULT_FAIL_LOG_BYTES = 2 * 16 * 1024 * 1024
SAMPLE_LIMIT = 40


def _python_root(runtime: Path) -> Path:
    return runtime / "python" if (runtime / "python").is_dir() else runtime


def _pyc_files(root: Path) -> dict[str, int]:
    """Every ``.pyc`` under ``root``, relative path to mtime in nanoseconds."""
    return {
        path.relative_to(root).as_posix(): path.stat().st_mtime_ns
        for path in root.rglob("*.pyc")
        if path.is_file()
    }


def _cache_tag(pyc: dict[str, int]) -> str:
    """The interpreter tag the shipped bytecode uses, e.g. ``cpython-314``."""
    tags = Counter(name.rsplit("/", 1)[-1].split(".")[1] for name in pyc if name.count(".") >= 2)
    return tags.most_common(1)[0][0] if tags else sys.implementation.cache_tag


def _category(relative: str) -> str:
    parts = relative.split("/")
    if "site-packages" not in parts:
        return "stdlib"
    package = parts[parts.index("site-packages") + 1 :][:1]
    return "first_party" if package and package[0] in FIRST_PARTY else "third_party"


def _stdlib_dir(root: Path) -> Path | None:
    lib = root / "Lib"
    if (lib / "os.py").is_file():
        return lib
    return next((path for path in sorted((root / "lib").glob("python3.*")) if path.is_dir()), None)


def _has_bytecode(source: Path, tag: str, pyc: set[str], root: Path) -> bool:
    cache = source.parent / "__pycache__"
    stem = f"{source.stem}.{tag}"
    return any(
        (cache / f"{stem}{suffix}").relative_to(root).as_posix() in pyc
        for suffix in (".pyc", ".opt-1.pyc", ".opt-2.pyc")
    )


def _writable(directory: Path) -> bool:
    """Whether this user can create a file in ``directory`` (a real probe, not ACL-blind)."""
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".clio-audit-"):
            return True
    except OSError:
        return False


def inventory(runtime: Path) -> dict[str, Any]:
    """Files, sizes and bytecode coverage of the installed runtime."""
    root = _python_root(runtime)
    pyc = _pyc_files(root)
    tag = _cache_tag(pyc)
    known = set(pyc)
    coverage: dict[str, dict[str, int]] = {}
    missing: dict[str, list[str]] = {}
    files = size = native = 0
    dist_infos = 0
    for path in runtime.rglob("*"):
        if path.is_dir():
            dist_infos += path.name.endswith(".dist-info")
            continue
        files += 1
        size += path.lstat().st_size
        native += path.name.endswith(NATIVE_SUFFIXES)
        if path.suffix != ".py" or not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix()
        bucket = coverage.setdefault(_category(relative), {"py": 0, "with_pyc": 0})
        bucket["py"] += 1
        if _has_bytecode(path, tag, known, root):
            bucket["with_pyc"] += 1
        else:
            missing.setdefault(_category(relative), []).append(relative)
    stdlib = _stdlib_dir(root)
    watched = {
        name: _has_bytecode(stdlib / name, tag, known, root)
        for name in WATCHED_STDLIB
        if stdlib is not None and (stdlib / name).is_file()
    }
    return {
        "runtime": str(runtime),
        "cache_tag": tag,
        "files": files,
        "bytes": size,
        "pyc_files": len(pyc),
        "native_libraries": native,
        "dist_infos": dist_infos,
        "python_root_writable": _writable(root),
        "coverage": coverage,
        "missing_sample": {key: sorted(value)[:SAMPLE_LIMIT] for key, value in missing.items()},
        "watched_stdlib_precompiled": watched,
        "_pyc": pyc,
    }


def drift(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    """Bytecode the launches wrote into the runtime after the snapshot."""
    created = sorted(set(after) - set(before))
    rewritten = sorted(name for name in set(after) & set(before) if after[name] != before[name])
    removed = sorted(set(before) - set(after))
    return {
        "created": len(created),
        "rewritten": len(rewritten),
        "removed": len(removed),
        "created_sample": created[:SAMPLE_LIMIT],
        "rewritten_sample": rewritten[:SAMPLE_LIMIT],
    }


def runtime_logs(roots: list[Path]) -> list[dict[str, Any]]:
    """Every clio-core daemon log (and flood record) under the given roots."""
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("clio-runtime*")):
            if path.is_file() and (".log" in path.name or path.name.endswith("flood.json")):
                found.append({"path": str(path), "bytes": path.stat().st_size})
    return found


def markdown(report: dict[str, Any]) -> str:
    """A step-summary table for the report."""
    lines = [
        f"### Installed runtime audit ({report['platform']})",
        "",
        f"`{report['runtime']}`: {report['files']:,} files, {report['bytes'] / 2**20:,.0f} MiB, "
        f"{report['pyc_files']:,} .pyc ({report['cache_tag']}), "
        f"{report['native_libraries']} native libraries, {report['dist_infos']} dist-infos; "
        f"python root writable by this user: **{report['python_root_writable']}**",
        "",
        "| Area | .py | with shipped .pyc | without |",
        "| --- | ---: | ---: | ---: |",
    ]
    for area, counts in sorted(report["coverage"].items()):
        lines.append(
            f"| {area} | {counts['py']:,} | {counts['with_pyc']:,} | "
            f"{counts['py'] - counts['with_pyc']:,} |"
        )
    watched = report["watched_stdlib_precompiled"]
    if watched:
        lines += [
            "",
            "#1979 modules precompiled: "
            + ", ".join(f"`{name}` {'yes' if ok else '**no**'}" for name, ok in watched.items()),
        ]
    if "drift" in report:
        moved = report["drift"]
        lines += [
            "",
            f"Bytecode written into the runtime by the launches: {moved['created']} created, "
            f"{moved['rewritten']} rewritten, {moved['removed']} removed.",
        ]
    logs = report["runtime_logs"]
    lines += [
        "",
        "clio-core daemon logs: "
        + (
            ", ".join(f"`{Path(item['path']).name}` {item['bytes']:,} B" for item in logs)
            or "none found"
        ),
    ]
    lines += ["", *(f"FAIL: {failure}" for failure in report["failures"])]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="record the runtime's bytecode before a launch")
    snap.add_argument("--runtime", type=Path, required=True)
    snap.add_argument("--out", type=Path, required=True)
    rep = sub.add_parser("report", help="measure coverage, drift and the daemon log")
    rep.add_argument("--runtime", type=Path, required=True)
    rep.add_argument("--snapshot", type=Path)
    rep.add_argument("--log-root", type=Path, action="append", default=[])
    rep.add_argument("--fail-log-bytes", type=int, default=DEFAULT_FAIL_LOG_BYTES)
    rep.add_argument("--out", type=Path)
    rep.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)

    if not (args.runtime / "runtime.json").is_file():
        parser.error(f"{args.runtime} has no runtime.json; not an installed desktop runtime")
    if args.command == "snapshot":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(_pyc_files(_python_root(args.runtime))), encoding="utf-8")
        return 0

    report = inventory(args.runtime)
    after = report.pop("_pyc")
    report["platform"] = sys.platform
    if args.snapshot is not None and args.snapshot.is_file():
        report["drift"] = drift(json.loads(args.snapshot.read_text(encoding="utf-8")), after)
    report["runtime_logs"] = runtime_logs(args.log_root)
    report["failures"] = [
        f"{item['path']} is {item['bytes']:,} bytes, over {args.fail_log_bytes:,}"
        for item in report["runtime_logs"]
        if ".log" in Path(item["path"]).name and item["bytes"] > args.fail_log_bytes
    ]
    text = markdown(report)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
