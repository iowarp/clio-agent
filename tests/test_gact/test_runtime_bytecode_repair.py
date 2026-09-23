"""The bundled-runtime stale-bytecode repair (0.9.4.14 -> 0.9.4.15 update brick)."""

from __future__ import annotations

import importlib.util
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.runtime_bytecode_repair import (
    MARKER_FILENAME,
    repair_runtime_bytecode,
)

_UNCHECKED = py_compile.PycInvalidationMode.UNCHECKED_HASH
_CHECKED = py_compile.PycInvalidationMode.CHECKED_HASH
_TIMESTAMP = py_compile.PycInvalidationMode.TIMESTAMP


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    """Build a fake bundled runtime: gact-runtime/{runtime.json,python/site-packages}."""

    root = tmp_path / "gact-runtime"
    prefix = root / "python"
    site_packages = prefix / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    (root / "runtime.json").write_text('{"schema":1}\n', encoding="utf-8")
    return prefix, site_packages


def _module(
    site_packages: Path,
    relative: str,
    source: str,
    mode: py_compile.PycInvalidationMode,
) -> tuple[Path, Path]:
    """Write a module source and compile it with ``mode``; return (source, pyc)."""

    path = site_packages / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    pyc = Path(importlib.util.cache_from_source(str(path)))
    py_compile.compile(str(path), cfile=str(pyc), doraise=True, invalidation_mode=mode)
    return path, pyc


def _dist_info(site_packages: Path, name: str, *, record: bool) -> Path:
    directory = site_packages / f"{name}.dist-info"
    directory.mkdir()
    (directory / "METADATA").write_text("Metadata-Version: 2.1\n", encoding="utf-8")
    if record:
        (directory / "RECORD").write_text("", encoding="utf-8")
    return directory


def test_upgraded_source_removes_the_stale_unchecked_bytecode(tmp_path: Path) -> None:
    """The field failure: new source, old unchecked bytecode that CPython would run."""

    prefix, site_packages = _runtime(tmp_path)
    source, pyc = _module(site_packages, "clio_schemas/__init__.py", "OLD = 1\n", _UNCHECKED)
    source.write_text("OLD = 1\nA2UIClientMessage = object\n", encoding="utf-8")

    report = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")

    assert report is not None
    assert report.removed_bytecode == [pyc]
    assert not pyc.exists()
    assert report.errors == []


def test_bytecode_still_matching_its_source_is_kept(tmp_path: Path) -> None:
    prefix, site_packages = _runtime(tmp_path)
    _source, pyc = _module(site_packages, "pkg/current.py", "VALUE = 2\n", _UNCHECKED)

    report = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")

    assert report is not None
    assert report.scanned_bytecode == 1
    assert report.removed_bytecode == []
    assert pyc.is_file()


@pytest.mark.parametrize("mode", [_CHECKED, _TIMESTAMP])
def test_self_validating_bytecode_is_never_touched(
    tmp_path: Path,
    mode: py_compile.PycInvalidationMode,
) -> None:
    """CPython re-validates checked-hash and timestamp bytecode on its own."""

    prefix, site_packages = _runtime(tmp_path)
    source, pyc = _module(site_packages, "pkg/validated.py", "VALUE = 3\n", mode)
    source.write_text("VALUE = 4\n", encoding="utf-8")

    report = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")

    assert report is not None
    assert report.removed_bytecode == []
    assert pyc.is_file()


def test_bytecode_whose_source_was_deleted_is_removed(tmp_path: Path) -> None:
    prefix, site_packages = _runtime(tmp_path)
    source, pyc = _module(site_packages, "clio_schemas/a2ui_v091.py", "X = 1\n", _UNCHECKED)
    source.unlink()

    report = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")

    assert report is not None
    assert report.removed_bytecode == [pyc]


def test_recordless_metadata_superseded_by_a_fresh_install_is_removed(tmp_path: Path) -> None:
    prefix, site_packages = _runtime(tmp_path)
    stale = _dist_info(site_packages, "clio_schemas-0.2.3", record=False)
    fresh = _dist_info(site_packages, "clio_schemas-0.3.2", record=True)
    lone = _dist_info(site_packages, "starlette-1.7.0", record=False)

    report = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")

    assert report is not None
    assert report.removed_metadata == [stale]
    assert not stale.exists()
    assert fresh.is_dir()
    assert lone.is_dir(), "a single metadata directory is never a duplicate"


def test_duplicate_metadata_without_a_single_recorded_winner_is_reported_not_removed(
    tmp_path: Path,
) -> None:
    prefix, site_packages = _runtime(tmp_path)
    first = _dist_info(site_packages, "pyjwt-2.14.0", record=False)
    second = _dist_info(site_packages, "pyjwt-2.15.0", record=False)

    report = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")

    assert report is not None
    assert report.removed_metadata == []
    assert report.ambiguous_metadata == ["pyjwt: 2 metadata directories, 0 with RECORD"]
    assert first.is_dir()
    assert second.is_dir()


def test_the_pass_runs_once_per_installed_version(tmp_path: Path) -> None:
    prefix, site_packages = _runtime(tmp_path)
    source, pyc = _module(site_packages, "pkg/mod.py", "A = 1\n", _UNCHECKED)

    first = repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")
    assert first is not None
    assert first.marker_written
    assert (prefix / MARKER_FILENAME).read_text(encoding="utf-8").strip() == "0.9.4.16"

    source.write_text("A = 2\n", encoding="utf-8")
    assert repair_runtime_bytecode(prefix, site_packages, "0.9.4.16") is None
    assert pyc.is_file(), "an already-verified version does not rescan"

    upgraded = repair_runtime_bytecode(prefix, site_packages, "0.9.4.17")
    assert upgraded is not None
    assert upgraded.removed_bytecode == [pyc]


def test_an_interpreter_without_a_runtime_manifest_is_left_alone(tmp_path: Path) -> None:
    """Developer venvs and pip installs are not bundled runtimes."""

    prefix = tmp_path / "venv"
    site_packages = prefix / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    source, pyc = _module(site_packages, "pkg/mod.py", "A = 1\n", _UNCHECKED)
    source.write_text("A = 2\n", encoding="utf-8")

    assert repair_runtime_bytecode(prefix, site_packages, "0.9.4.16") is None
    assert pyc.is_file()
    assert not (prefix / MARKER_FILENAME).exists()


def test_stale_bytecode_really_runs_old_code_until_repaired(tmp_path: Path) -> None:
    """Prove the failure mode on the real interpreter, then the repair.

    CPython executes the unchecked bytecode even though the source changed;
    after the repair the same import sees the new source.
    """

    prefix, site_packages = _runtime(tmp_path)
    source, _pyc = _module(site_packages, "stalemod/__init__.py", "VALUE = 'old'\n", _UNCHECKED)
    source.write_text("VALUE = 'new'\n", encoding="utf-8")
    probe = [sys.executable, "-B", "-c", "import stalemod; print(stalemod.VALUE)"]
    # The suite redirects bytecode with PYTHONPYCACHEPREFIX; the bundled
    # runtime reads the in-tree __pycache__, so the probe must too.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPYCACHEPREFIX"}
    env["PYTHONPATH"] = str(site_packages)

    def _import_value() -> str:
        completed = subprocess.run(
            probe,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    assert _import_value() == "old"
    repair_runtime_bytecode(prefix, site_packages, "0.9.4.16")
    assert _import_value() == "new"
