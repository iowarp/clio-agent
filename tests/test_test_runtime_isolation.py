"""Regression tests for repository-contained pytest runtime state."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import psutil
import pytest

from tests._test_runtime_isolation import (
    cleanup_test_runtime,
    create_test_runtime,
    resolve_test_runtime_parent,
    stale_test_runtimes,
)


def test_default_runtime_stays_inside_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    environment: dict[str, str] = {}

    runtime = create_test_runtime(checkout, environment, pid=123, nonce="unit")

    assert runtime.parent == checkout / ".pytest-runtime"
    assert runtime.parent.is_relative_to(checkout)
    assert runtime.root == runtime.parent / "run-123-unit"
    assert runtime.temp_dir.is_relative_to(runtime.root)
    assert runtime.pytest_dir.is_relative_to(runtime.root)
    assert environment["TEMP"] == str(runtime.temp_dir)
    assert environment["TMP"] == str(runtime.temp_dir)
    assert environment["TMPDIR"] == str(runtime.temp_dir)
    assert environment["CLIO_TEST_RUNTIME_DIR"] == str(runtime.root)
    for name in (
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "PYTHONPYCACHEPREFIX",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "CLIO_USER_DIR",
        "CLIO_RUNTIME_STATE_DIR",
    ):
        assert Path(environment[name]).is_relative_to(runtime.root)


@pytest.mark.parametrize("configured", ["..", "../external-state", "."])
def test_runtime_rejects_escaping_or_checkout_root_override(
    tmp_path: Path, configured: str
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    with pytest.raises(ValueError, match="inside the checkout"):
        create_test_runtime(checkout, {"CLIO_TEST_RUNTIME_ROOT": configured})

    assert list(checkout.iterdir()) == []
    assert not (tmp_path / "external-state").exists()


def test_runtime_rejects_absolute_external_override(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "external-state"

    with pytest.raises(ValueError, match="inside the checkout"):
        create_test_runtime(checkout, {"CLIO_TEST_RUNTIME_ROOT": str(external)})

    assert not external.exists()


def test_runtime_accepts_repository_local_override(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    parent = resolve_test_runtime_parent(checkout, {"CLIO_TEST_RUNTIME_ROOT": ".test-state"})

    assert parent == checkout / ".test-state"


def test_runtime_cleanup_removes_readonly_fixture_files(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    runtime = create_test_runtime(checkout, {}, pid=456, nonce="readonly")
    readonly = runtime.temp_dir / "registry" / ".git" / "objects" / "aa" / "object"
    readonly.parent.mkdir(parents=True)
    readonly.write_bytes(b"fixture")
    readonly.chmod(stat.S_IREAD)

    cleanup_test_runtime(runtime.root, runtime.parent, retry_delay_seconds=0)

    assert not runtime.root.exists()


def test_runtime_cleanup_removes_deep_plugin_cache_paths(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    runtime = create_test_runtime(checkout, {}, pid=457, nonce="deep-cache")
    deep_file = runtime.temp_dir.joinpath(*(["plugin-cache"] * 24), "payload.pyc")
    resolved = str(deep_file.resolve())
    creation_path = Path(f"\\\\?\\{resolved}") if os.name == "nt" else deep_file
    creation_path.parent.mkdir(parents=True)
    creation_path.write_bytes(b"fixture")
    assert len(resolved) > 260

    cleanup_test_runtime(runtime.root, runtime.parent, retry_delay_seconds=0)

    assert not runtime.root.exists()


def test_only_dead_owned_runs_are_stale(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    dead = create_test_runtime(checkout, {}, pid=111, nonce="dead")
    live = create_test_runtime(checkout, {}, pid=222, nonce="live")
    unrelated = dead.parent / "operator-data"
    unrelated.mkdir()

    stale = stale_test_runtimes(
        dead.parent,
        pid_is_live=lambda pid: pid == 222,
    )

    assert stale == [dead.root]
    assert live.root not in stale
    assert unrelated not in stale


def test_live_suite_runtime_is_not_system_temp(pytestconfig: pytest.Config) -> None:
    runtime = Path(os.environ["CLIO_TEST_RUNTIME_DIR"]).resolve()
    checkout = Path(__file__).resolve().parents[1]

    # Overrides may select another directory, but cannot escape the checkout.
    expected_parent = resolve_test_runtime_parent(checkout, os.environ)
    assert runtime.is_relative_to(expected_parent)
    assert runtime.is_relative_to(checkout)
    assert Path(os.environ["TEMP"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["TMP"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["TMPDIR"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["UV_CACHE_DIR"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["PIP_CACHE_DIR"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["XDG_CACHE_HOME"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["XDG_STATE_HOME"]).resolve().is_relative_to(runtime)
    assert pytestconfig.cache is not None
    # xdist inherits the controller's parsed cache option before worker bootstrap.
    # Its cache belongs to that same launch; all worker runtime state stays private.
    controller = Path(os.environ.get("CLIO_TEST_CONTROLLER_RUNTIME", str(runtime))).resolve()
    assert controller.is_relative_to(expected_parent)
    assert controller.is_relative_to(checkout)
    owner = json.loads((controller / ".clio-test-run.json").read_text(encoding="utf-8"))
    assert Path(owner["checkout"]).resolve() == checkout
    assert controller.name.startswith(f"run-{owner['pid']}-")
    assert pytestconfig.cache._cachedir.resolve().is_relative_to(controller)


def test_launcher_isolates_plugin_imports_and_cleans_help_exit(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    probe = tmp_path / "plugin-result.json"
    runtime_parent = tmp_path / "bootstrap-runs"
    plugin = tmp_path / "clio_bootstrap_probe.py"
    plugin.write_text(
        "import json, os, tempfile\n"
        "from pathlib import Path\n"
        "names = ['TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE', 'CLIO_USER_DIR', "
        "'CLIO_RUNTIME_STATE_DIR', 'UV_CACHE_DIR', 'XDG_CONFIG_HOME', 'LOCALAPPDATA']\n"
        "Path(os.environ['CLIO_BOOTSTRAP_PROBE_PATH']).write_text(json.dumps({"
        "'root': os.environ['CLIO_TEST_RUNTIME_DIR'], 'tempfile': tempfile.gettempdir(), "
        "'paths': {name: os.environ[name] for name in names}, 'user': os.environ['USER']}))\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "CLIO_TEST_RUNTIME_ROOT": str(runtime_parent),
        "CLIO_BOOTSTRAP_PROBE_PATH": str(probe),
        "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(checkout / "scripts/run_tests.py"),
            "--help",
            # This probe targets pre-plugin containment, not application fixture startup.
            "--noconftest",
            "-p",
            "clio_bootstrap_probe",
        ],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(probe.read_text(encoding="utf-8"))
    root = Path(evidence["root"])
    assert root.is_relative_to(checkout)
    assert all(Path(path).is_relative_to(root) for path in evidence["paths"].values())
    assert Path(evidence["tempfile"]).is_relative_to(root)
    assert evidence["user"] != os.environ["USER"]
    assert not root.exists()


def test_cleanup_removes_readonly_snapshot_directories(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    runtime = create_test_runtime(checkout, {}, pid=458, nonce="snapshot")
    snapshot = runtime.pytest_dir / "source" / "snapshot"
    target = snapshot / "OPAL" / "poisoned" / "data.csv"
    target.parent.mkdir(parents=True)
    target.write_text("input,value\na,1\n", encoding="utf-8")
    target.chmod(0o444)
    for directory in (target.parent, target.parent.parent, snapshot):
        directory.chmod(0o555)
    cleanup_test_runtime(runtime.root, runtime.parent, retry_delay_seconds=0)
    assert not runtime.root.exists()


def _launcher() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py"
    spec = importlib.util.spec_from_file_location("clio_run_tests_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_zombie(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.ZombieProcess:
        return True
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows has no zombie processes")
def test_launcher_does_not_count_an_exited_unreaped_child_as_active() -> None:
    """A zombie (e.g. a log pump that exited at EOF) must not retain the run's state."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv
    try:
        deadline = time.monotonic() + 20
        while not _is_zombie(child.pid):
            assert time.monotonic() < deadline, "child never exited"
            time.sleep(0.05)
        survivors = _launcher().active_children(grace_s=0)
        assert not any(entry.startswith(f"pid={child.pid} ") for entry in survivors)
    finally:
        child.wait(timeout=10)


def test_launcher_names_a_child_that_is_still_running() -> None:
    child = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        survivors = _launcher().active_children(grace_s=0)
        assert any(entry.startswith(f"pid={child.pid} ") for entry in survivors), survivors
    finally:
        child.kill()
        child.wait(timeout=10)
