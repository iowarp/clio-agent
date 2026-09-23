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
repair runs once per installed clio-agent version (a marker file in the runtime
records the last verified version) and:

* deletes unchecked-hash ``.pyc`` files whose embedded source hash no longer
  matches the ``.py`` beside them, or whose source is gone; Python recompiles
  the modules that are still imported;
* removes ``*.dist-info`` directories left without ``RECORD`` when a sibling
  directory for the same distribution (the one ``uv`` just wrote) has one.

Every action is reported on stderr, which the desktop tees into its boot log.
This module imports only the standard library: it runs before any dependency
that might itself be stale.
"""

from __future__ import annotations

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


def _repair_metadata(site_packages: Path, report: BytecodeRepairReport) -> None:
    """Remove RECORD-less dist-info dirs superseded by a freshly installed sibling."""

    groups: dict[str, list[Path]] = {}
    for entry in site_packages.glob("*.dist-info"):
        if entry.is_dir():
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
        for stale in entries:
            if stale == recorded[0]:
                continue
            try:
                shutil.rmtree(stale)
                report.removed_metadata.append(stale)
            except OSError as error:
                report.errors.append(f"metadata {stale}: {error}")


def repair_runtime_bytecode(
    prefix: Path,
    site_packages: Path,
    version: str,
) -> BytecodeRepairReport | None:
    """Repair a bundled runtime once for ``version``.

    Args:
        prefix: The runtime interpreter prefix (``gact-runtime/python``).
        site_packages: The prefix's ``site-packages`` directory.
        version: The installed clio-agent version the pass verifies.

    Returns:
        The pass report, or ``None`` when ``prefix`` is not a bundled runtime or
        this version was already verified.
    """

    if not (prefix.parent / RUNTIME_MANIFEST).is_file():
        return None
    marker = prefix / MARKER_FILENAME
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == version:
                return None
        except OSError as error:
            print(f"{_REPORT_PREFIX} marker unreadable ({error}); verifying", file=sys.stderr)

    report = BytecodeRepairReport(version=version)
    if site_packages.is_dir():
        _repair_bytecode(site_packages, report)
        _repair_metadata(site_packages, report)
    if not report.errors:
        try:
            marker.write_text(f"{version}\n", encoding="utf-8")
            report.marker_written = True
        except OSError as error:
            report.errors.append(f"marker {marker}: {error}")
    return report


def emit_report(report: BytecodeRepairReport) -> None:
    """Write the repair outcome to stderr (the desktop boot log)."""

    print(
        f"{_REPORT_PREFIX} version={report.version} scanned_bytecode={report.scanned_bytecode} "
        f"removed_stale_bytecode={len(report.removed_bytecode)} "
        f"removed_stale_metadata={len(report.removed_metadata)} errors={len(report.errors)}",
        file=sys.stderr,
        flush=True,
    )
    for path in report.removed_metadata:
        print(f"{_REPORT_PREFIX} removed superseded metadata {path.name}", file=sys.stderr)
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
