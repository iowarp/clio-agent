"""Tests for the installed-runtime bytecode and daemon-log audit (#1979, #1983)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "runtime_bytecode_audit.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_bytecode_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load()


def _touch(path: Path, data: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    return path


@pytest.fixture()
def runtime(tmp_path: Path) -> Path:
    """A POSIX-layout runtime: the build precompiled os and clio_agent, not plistlib."""
    root = tmp_path / "gact-runtime"
    _touch(root / "runtime.json", "{}")
    lib = root / "python" / "lib" / "python3.14"
    _touch(lib / "os.py")
    _touch(lib / "__pycache__" / "os.cpython-314.pyc")
    _touch(lib / "plistlib.py")
    site = lib / "site-packages"
    _touch(site / "clio_agent" / "app.py")
    _touch(site / "clio_agent" / "__pycache__" / "app.cpython-314.opt-1.pyc")
    _touch(site / "litellm" / "lazy.py")
    _touch(root / "python" / "lib" / "libpython3.14.so")
    (site / "litellm-1.0.dist-info").mkdir()
    return root


def test_inventory_counts_coverage_by_area(runtime: Path) -> None:
    report = audit.inventory(runtime)

    assert report["cache_tag"] == "cpython-314"
    assert report["coverage"] == {
        "stdlib": {"py": 2, "with_pyc": 1},
        "first_party": {"py": 1, "with_pyc": 1},
        "third_party": {"py": 1, "with_pyc": 0},
    }
    assert report["missing_sample"]["stdlib"] == ["lib/python3.14/plistlib.py"]
    assert report["watched_stdlib_precompiled"] == {"plistlib.py": False}
    assert report["native_libraries"] == 1
    assert report["dist_infos"] == 1


def test_report_names_bytecode_a_launch_wrote_and_passes(runtime: Path, tmp_path: Path) -> None:
    snapshot = tmp_path / "before.json"
    assert audit.main(["snapshot", "--runtime", str(runtime), "--out", str(snapshot)]) == 0
    _touch(runtime / "python/lib/python3.14/__pycache__/plistlib.cpython-314.pyc")
    out, summary = tmp_path / "report.json", tmp_path / "summary.md"

    code = audit.main(
        ["report", "--runtime", str(runtime), "--snapshot", str(snapshot)]
        + ["--out", str(out), "--summary", str(summary)]
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0
    assert report["drift"]["created"] == 1
    assert report["drift"]["created_sample"] == [
        "lib/python3.14/__pycache__/plistlib.cpython-314.pyc"
    ]
    assert "1 created" in summary.read_text(encoding="utf-8")


def test_an_oversized_daemon_log_fails_the_report(runtime: Path, tmp_path: Path) -> None:
    state = tmp_path / "agent" / "state" / "core-hosts" / "runner"
    _touch(state / "clio-runtime.log", "x" * 64)
    _touch(state / "clio-runtime.log.1", "x" * 8)

    code = audit.main(
        ["report", "--runtime", str(runtime), "--log-root", str(tmp_path / "agent")]
        + ["--fail-log-bytes", "32"]
    )

    assert code == 1


def test_a_directory_without_runtime_json_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        audit.main(["report", "--runtime", str(tmp_path)])
