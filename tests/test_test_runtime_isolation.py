"""Regression tests for repository-contained pytest runtime state."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from tests._test_runtime_isolation import (
    cleanup_test_runtime,
    create_test_runtime,
    stale_test_runtimes,
)


def test_default_runtime_stays_under_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    environment: dict[str, str] = {}

    runtime = create_test_runtime(checkout, environment, pid=123, nonce="unit")

    assert runtime.parent == checkout / ".pytest-runtime"
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
    ):
        assert Path(environment[name]).is_relative_to(runtime.root)


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


def test_live_suite_runtime_is_not_system_temp() -> None:
    runtime = Path(os.environ["CLIO_TEST_RUNTIME_DIR"]).resolve()
    checkout = Path(__file__).resolve().parents[1]

    assert runtime.is_relative_to(checkout / ".pytest-runtime")
    assert Path(os.environ["TEMP"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["TMP"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["TMPDIR"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["UV_CACHE_DIR"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["PIP_CACHE_DIR"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["XDG_CACHE_HOME"]).resolve().is_relative_to(runtime)
    assert Path(os.environ["XDG_STATE_HOME"]).resolve().is_relative_to(runtime)
