"""One-shot repair of stale bytecode in an in-place-upgraded bundled runtime.

The desktop bundles a relocatable Python runtime (``gact-runtime/``, marked by
its ``runtime.json`` manifest) and upgrades it in place with ``uv pip install``.
Runtimes built through 0.9.4.15 shipped UNCHECKED-hash bytecode and no
``dist-info/RECORD`` files, so ``uv`` could not remove the previous release's
files. CPython never re-validates unchecked bytecode against its source, so
after an upgrade the dependencies kept running the previous release's code
(``cannot import name 'A2UIClientMessage' from 'clio_schemas'`` — the backend
then exits before answering ``/v1/capabilities``).

Desktops already in the field run that same upgrade, so the fix cannot live
only in the runtime build or the updater: ``python -m clio_agent.gact`` calls
:func:`repair_bundled_runtime_bytecode` before importing the application. The
repair runs once per installed package set (a marker file in the runtime holds a
fingerprint of the installed distributions) and:

* for a distribution left with a RECORD-less old ``*.dist-info`` beside the
  one ``uv`` just wrote, deletes the files the old version left inside the new
  version's own packages (anything the new RECORD does not list), then the old
  ``*.dist-info`` itself;
* deletes unchecked-hash ``.pyc`` files whose embedded source hash no longer
  matches the ``.py`` beside them, or whose source is gone; Python recompiles
  the modules that are still imported.

Every action is reported on stderr, which the desktop tees into its boot log.
This module imports only the standard library: it runs before any dependency
that might itself be stale.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

RUNTIME_MANIFEST = "runtime.json"
MARKER_FILENAME = ".clio-bytecode-verified"
_PYC_HEADER_BYTES = 16
_FLAG_HASH_BASED = 0b01
_FLAG_CHECK_SOURCE = 0b10
_REPORT_PREFIX = "clio-runtime-repair:"


@dataclass
class BytecodeRepairReport:
    """What one repair pass inspected, removed, and failed to remove."""

    version: str
    scanned_bytecode: int = 0
    removed_bytecode: list[Path] = field(default_factory=list)
    removed_metadata: list[Path] = field(default_factory=list)
    removed_files: list[Path] = field(default_factory=list)
    ambiguous_metadata: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    marker_written: bool = False


def _stale_unchecked_bytecode(pyc: Path) -> bool:
    """Return whether ``pyc`` is unchecked-hash bytecode that no longer fits its source."""

    with pyc.open("rb") as handle:
        header = handle.read(_PYC_HEADER_BYTES)
    if len(header) < _PYC_HEADER_BYTES or header[:4] != importlib.util.MAGIC_NUMBER:
        # Another interpreter's bytecode: CPython already ignores and rewrites it.
        return False
    flags = int.from_bytes(header[4:8], "little")
    if not flags & _FLAG_HASH_BASED or flags & _FLAG_CHECK_SOURCE:
        # Timestamp and checked-hash bytecode are validated by CPython itself.
        return False
    try:
        source = Path(importlib.util.source_from_cache(str(pyc)))
    except ValueError:
        # Not a PEP 3147 cache path; CPython never loads it for a source module.
        return False
    if not source.is_file():
        return True
    return importlib.util.source_hash(source.read_bytes()) != header[8:16]


def _repair_bytecode(site_packages: Path, report: BytecodeRepairReport) -> None:
    """Delete stale unchecked-hash bytecode below ``site_packages``."""

    for directory, _subdirectories, files in os.walk(site_packages):
        if Path(directory).name != "__pycache__":
            continue
        for name in files:
            if not name.endswith(".pyc"):
                continue
            pyc = Path(directory) / name
            report.scanned_bytecode += 1
            try:
                if _stale_unchecked_bytecode(pyc):
                    pyc.unlink()
                    report.removed_bytecode.append(pyc)
            except OSError as error:
                report.errors.append(f"bytecode {pyc}: {error}")


def _distribution_key(dist_info: Path) -> str:
    """Return the normalized distribution name of a ``name-version.dist-info`` dir."""

    stem = dist_info.name.removesuffix(".dist-info")
    return stem.partition("-")[0].replace("-", "_").replace(".", "_").lower()


def _normalized(path: Path) -> str:
    """Return a comparison key for a filesystem path (case-folded on Windows)."""

    return os.path.normcase(os.path.normpath(path))


def _record_paths(site_packages: Path, dist_info: Path) -> set[str]:
    """Return the normalized absolute paths a distribution's RECORD lists."""

    paths: set[str] = set()
    with (dist_info / "RECORD").open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if row and row[0]:
                paths.add(_normalized(site_packages / row[0]))
    return paths


def _claimed_top_levels(dist_infos: list[Path]) -> set[str]:
    """Return the top-level names other distributions declare in top_level.txt."""

    claimed: set[str] = set()
    for dist_info in dist_infos:
        top_level = dist_info / "top_level.txt"
        if top_level.is_file():
            claimed.update(
                line.strip().lower()
                for line in top_level.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    return claimed


def _remove_leftover_files(
    site_packages: Path,
    winner: Path,
    others: list[Path],
    report: BytecodeRepairReport,
) -> None:
    """Delete files the superseded version left inside the new version's packages.

    Without RECORD, ``uv`` cannot uninstall the previous version, so modules and
    data it had but the new version dropped stay on disk (an old ``pkg/x/``
    package would even shadow a new ``pkg/x.py``). The freshly installed
    version's RECORD is the ground truth: inside each REGULAR package it owns
    (its ``__init__.py`` is in RECORD, so not a shared namespace package) and
    that no other distribution declares, every file RECORD does not list is a
    leftover. Bytecode caches are left to the bytecode pass.
    """

    recorded = _record_paths(site_packages, winner)
    claimed = _claimed_top_levels(others)
    base = Path(_normalized(site_packages))
    roots: set[str] = set()
    for entry in recorded:
        if not Path(entry).is_relative_to(base):
            continue  # e.g. ../../Scripts/tool.exe: outside site-packages
        relative = Path(entry).relative_to(base)
        if len(relative.parts) < 2 or relative.parts[0].endswith((".dist-info", ".data")):
            continue
        roots.add(relative.parts[0])
    for root in sorted(roots):
        package = site_packages / root
        if root.lower() in claimed or _normalized(package / "__init__.py") not in recorded:
            continue
        for directory, subdirectories, files in os.walk(package):
            subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
            for name in files:
                path = Path(directory) / name
                if _normalized(path) in recorded:
                    continue
                try:
                    path.unlink()
                    report.removed_files.append(path)
                except OSError as error:
                    report.errors.append(f"leftover {path}: {error}")


def _repair_metadata(site_packages: Path, report: BytecodeRepairReport) -> None:
    """Remove what a superseded, RECORD-less version left beside a fresh install."""

    dist_infos = [entry for entry in site_packages.glob("*.dist-info") if entry.is_dir()]
    groups: dict[str, list[Path]] = {}
    for entry in dist_infos:
        groups.setdefault(_distribution_key(entry), []).append(entry)
    for name, entries in sorted(groups.items()):
        if len(entries) < 2:
            continue
        recorded = [entry for entry in entries if (entry / "RECORD").is_file()]
        if len(recorded) != 1:
            report.ambiguous_metadata.append(
                f"{name}: {len(entries)} metadata directories, {len(recorded)} with RECORD"
            )
            continue
        winner = recorded[0]
        others = [entry for entry in dist_infos if entry not in entries]
        try:
            _remove_leftover_files(site_packages, winner, others, report)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            report.errors.append(f"leftovers of {name}: {error}")
        for stale in entries:
            if stale == winner:
                continue
            try:
                shutil.rmtree(stale)
                report.removed_metadata.append(stale)
            except OSError as error:
                report.errors.append(f"metadata {stale}: {error}")


def _installed_fingerprint(site_packages: Path) -> str:
    """Hash the installed distribution set (every ``*.dist-info`` directory name)."""

    names = sorted(entry.name for entry in site_packages.glob("*.dist-info"))
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def repair_runtime_bytecode(
    prefix: Path,
    site_packages: Path,
    version: str,
) -> BytecodeRepairReport | None:
    """Repair a bundled runtime once per installed package set.

    Args:
        prefix: The runtime interpreter prefix (``gact-runtime/python``).
        site_packages: The prefix's ``site-packages`` directory.
        version: The installed clio-agent version, reported with the pass.

    Returns:
        The pass report, or ``None`` when ``prefix`` is not a bundled runtime or
        this exact set of installed distributions was already verified.
    """

    if not (prefix.parent / RUNTIME_MANIFEST).is_file() or not site_packages.is_dir():
        return None
    marker = prefix / MARKER_FILENAME
    # Keyed on every installed distribution, not the clio-agent version alone:
    # a retried update can change a dependency without changing clio-agent.
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == _installed_fingerprint(site_packages):
                return None
        except OSError as error:
            print(f"{_REPORT_PREFIX} marker unreadable ({error}); verifying", file=sys.stderr)

    report = BytecodeRepairReport(version=version)
    # Leftover modules first, so their now-orphaned bytecode is swept below.
    _repair_metadata(site_packages, report)
    _repair_bytecode(site_packages, report)
    if not report.errors:
        try:
            marker.write_text(f"{_installed_fingerprint(site_packages)}\n", encoding="utf-8")
            report.marker_written = True
        except OSError as error:
            report.errors.append(f"marker {marker}: {error}")
    return report


def emit_report(report: BytecodeRepairReport) -> None:
    """Write the repair outcome to stderr (the desktop boot log)."""

    print(
        f"{_REPORT_PREFIX} version={report.version} scanned_bytecode={report.scanned_bytecode} "
        f"removed_stale_bytecode={len(report.removed_bytecode)} "
        f"removed_stale_metadata={len(report.removed_metadata)} "
        f"removed_leftover_files={len(report.removed_files)} errors={len(report.errors)}",
        file=sys.stderr,
        flush=True,
    )
    for path in report.removed_metadata:
        print(f"{_REPORT_PREFIX} removed superseded metadata {path.name}", file=sys.stderr)
    for path in report.removed_files:
        print(f"{_REPORT_PREFIX} removed leftover file {path}", file=sys.stderr)
    for detail in report.ambiguous_metadata:
        print(f"{_REPORT_PREFIX} left ambiguous metadata {detail}", file=sys.stderr)
    for detail in report.errors:
        print(f"{_REPORT_PREFIX} error {detail}", file=sys.stderr)


def repair_bundled_runtime_bytecode() -> BytecodeRepairReport | None:
    """Run the one-shot repair for the running interpreter, if it is a bundled runtime."""

    from clio_agent import __version__  # noqa: PLC0415 - stdlib-light package root

    report = repair_runtime_bytecode(
        Path(sys.prefix),
        Path(sysconfig.get_path("purelib")),
        __version__,
    )
    if report is not None:
        emit_report(report)
    return report
