"""The cleanup owner never guesses names or removes active, nonempty or original metadata."""

from __future__ import annotations

import json
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clio_agent.runtime import codex_metadata as metadata

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Codex bwrap is Linux-only")

SYNTHESIZE = """
import os, pathlib, sys, time
for directory in sys.argv[1:]:
    root = pathlib.Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for name in (".git", ".agents", ".codex", ".aws"):
        target = root / name
        if not os.path.lexists(target):
            target.mkdir()
print("ready", flush=True)
time.sleep(60)
"""


def _argv(root: Path, markers: Path, code: str, *more: Path) -> list[str]:
    command, args = metadata.wrapper_argv(
        sys.executable,
        ["-B", "-u", "-c", code, str(root), *map(str, more)],
        [root, *more],
        markers,
    )
    return [command, *args]


def _start(root: Path, markers: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        _argv(root, markers, SYNTHESIZE),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        assert select.select([process.stdout], [], [], 10)[0], "cleanup supervisor did not start"
        assert process.stdout.readline().strip() == "ready"
    except BaseException:
        process.kill()
        process.wait(timeout=10)
        raise
    return process


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()


def _kill(process: subprocess.Popen[str], markers: Path) -> dict:
    data = json.loads(next(markers.glob("*.json")).read_text())
    child = data["owners"][0]["child"]
    process.kill()
    assert process.wait(timeout=10) == -signal.SIGKILL
    deadline = time.monotonic() + 10
    while metadata._process_identity(child[0]) == child and time.monotonic() < deadline:
        time.sleep(0.01)
    assert metadata._process_identity(child[0]) != child
    return data


def test_exit_cleans_only_recorded_empty_directories_and_preserves_exit_code(
    tmp_path: Path,
) -> None:
    root, second = tmp_path / "workspace with spaces", tmp_path / "new root"
    root.mkdir()
    (root / ".aws").mkdir()
    (root / ".git").write_text("gitdir: elsewhere\n")
    destination = tmp_path / "destination"
    destination.mkdir()
    (root / ".agents").symlink_to(destination, target_is_directory=True)
    markers = tmp_path / "instance" / ".codex" / "markers"
    code = SYNTHESIZE.replace(
        "time.sleep(60)",
        """
(pathlib.Path(sys.argv[1]) / ".codex" / "keep.txt").write_text("user data")
sys.exit(7)
""",
    )
    result = subprocess.run(
        _argv(root, markers, code, second),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 7, result.stderr
    assert (root / ".aws").is_dir()
    assert (root / ".git").is_file()
    assert (root / ".agents").is_symlink() and destination.is_dir()
    assert (root / ".codex" / "keep.txt").read_text() == "user data"
    assert not any((second / name).exists() for name in metadata.PROTECTED_NAMES)
    assert not list(markers.glob("*.json"))


def test_signal_exit_cleans_but_concurrent_live_children_keep_their_targets(tmp_path: Path) -> None:
    root, markers = tmp_path / "root", tmp_path / "instance" / ".codex" / "markers"
    root.mkdir()
    first = _start(root, markers)
    second = None
    try:
        second = _start(root, markers)
        first.terminate()
        assert first.wait(timeout=10) == -signal.SIGTERM
        assert all((root / name).is_dir() for name in metadata.PROTECTED_NAMES)
        second.terminate()
        assert second.wait(timeout=10) == -signal.SIGTERM
        assert not any((root / name).exists() for name in metadata.PROTECTED_NAMES)
        assert not list(markers.glob("*.json"))
    finally:
        _stop(first)
        if second is not None:
            _stop(second)


def test_sigkill_stale_markers_are_swept_before_the_next_child_starts(tmp_path: Path) -> None:
    root, markers = tmp_path / "root", tmp_path / "instance" / ".codex" / "markers"
    root.mkdir()
    process = _start(root, markers)
    try:
        data = _kill(process, markers)
        assert set(data["absent"]) == set(metadata.PROTECTED_NAMES)
        assert all((root / name).is_dir() for name in metadata.PROTECTED_NAMES)
        code = """
import pathlib, sys
root = pathlib.Path(sys.argv[1])
assert not any((root / name).exists() for name in (".git", ".agents", ".codex", ".aws"))
"""
        result = subprocess.run(
            _argv(root, markers, code), capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        assert not list(markers.glob("*.json"))
    finally:
        _stop(process)


def test_replaced_root_is_not_mistaken_for_the_recorded_root(tmp_path: Path) -> None:
    root, markers = tmp_path / "root", tmp_path / "instance" / ".codex" / "markers"
    root.mkdir()
    process = _start(root, markers)
    try:
        _kill(process, markers)
        root.rename(tmp_path / "original")
        root.mkdir()
        (root / ".aws").mkdir()
        result = subprocess.run(
            _argv(root, markers, "pass"), capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        assert "codex_metadata_root_replaced" in result.stderr
        assert (root / ".aws").is_dir()
        assert (tmp_path / "original" / ".aws").is_dir()
    finally:
        _stop(process)


def test_corrupt_marker_fails_loud_without_sweeping_or_starting_the_child(tmp_path: Path) -> None:
    root, markers = tmp_path / "root", tmp_path / "instance" / ".codex" / "markers"
    root.mkdir()
    process = _start(root, markers)
    try:
        data = _kill(process, markers)
        data["absent"].append("ordinary")
        next(markers.glob("*.json")).write_text(json.dumps(data))
        result = subprocess.run(
            _argv(root, markers, 'raise AssertionError("must not start")'),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0 and "codex_metadata_cleanup_failed" in result.stderr
        assert all((root / name).is_dir() for name in metadata.PROTECTED_NAMES)
    finally:
        _stop(process)


def test_marker_store_may_not_be_inside_unprotected_child_writes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="read-only"):
        metadata.wrapper_argv("codex", [], [tmp_path], tmp_path / "markers")


def test_symlinked_instance_home_is_canonicalized_before_launch(tmp_path: Path) -> None:
    root, home = tmp_path / "root", tmp_path / "real home"
    root.mkdir()
    home.mkdir()
    alias = tmp_path / "home alias"
    alias.symlink_to(home, target_is_directory=True)
    result = subprocess.run(
        _argv(root, alias / ".codex" / "markers", "pass"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (home / ".codex" / "markers").is_dir()


def test_symlinked_marker_store_cannot_bypass_the_read_only_guard(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "ordinary"
    target.mkdir(parents=True)
    alias = tmp_path / "home alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="read-only"):
        metadata.wrapper_argv("codex", [], [root], alias / ".codex" / "markers")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_cleanup_failure_is_explicit_and_preserves_a_failed_child_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exit_code: int,
) -> None:
    root, markers = tmp_path / "root", tmp_path / "instance" / ".codex" / "markers"
    root.mkdir()

    def cannot_finish(_marker: metadata._Marker, _token: str) -> None:
        raise PermissionError("cannot update ownership marker")

    monkeypatch.setattr(metadata._Marker, "finish", cannot_finish)
    command = [sys.executable, "-B", "-c", f"raise SystemExit({exit_code})"]
    if exit_code == 0:
        with pytest.raises(ExceptionGroup, match="cleanup failed"):
            metadata.run(markers, [root], command)
    else:
        assert metadata.run(markers, [root], command) == exit_code
    assert f"codex_metadata_cleanup_failed child_exit_code={exit_code}" in caplog.text
