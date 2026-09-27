"""State files survive another process holding them open during a write.

Live on ares (2026-09-27) a deploy failed with WinError 5 because a monitoring
script had ``infrastructure.json`` open while the store replaced it. On
Windows, a reader that did not share delete access blocks ``os.replace`` onto
the file; POSIX never does. These tests hold the file open for real (a plain
``open()`` on Windows does not share delete) and assert the write lands.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from clio_agent import platform_paths
from clio_agent.gact.infrastructure.models import CreateTargetRequest, SshRoute
from clio_agent.gact.infrastructure.store import InfrastructureStore


def _staged(directory: Path) -> list[str]:
    """Leftover staged or temp files (a partial write left behind)."""

    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


@contextmanager
def held_open(path: Path, seconds: float) -> Iterator[None]:
    """Keep ``path`` open for reading for ``seconds`` in another thread."""

    opened = threading.Event()

    def hold() -> None:
        with open(path, encoding="utf-8"):
            opened.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    opened.wait()
    try:
        yield
    finally:
        thread.join()


def test_the_infrastructure_store_writes_while_a_reader_holds_the_file(tmp_path: Path) -> None:
    path = tmp_path / "infrastructure.json"
    store = InfrastructureStore(path)
    store.create_target(CreateTargetRequest(label="first", kind="ssh", ssh=SshRoute(host="a")))

    with held_open(path, 0.8):
        store.create_target(CreateTargetRequest(label="second", kind="ssh", ssh=SshRoute(host="b")))

    labels = {row.label for row in InfrastructureStore(path).targets()}
    assert {"first", "second"} <= labels
    assert _staged(tmp_path) == []


def test_atomic_write_text_lands_while_a_reader_holds_the_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    with held_open(path, 0.8):
        platform_paths.atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert _staged(tmp_path) == []


def test_a_write_blocked_past_the_budget_is_typed_and_leaves_no_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_paths.time, "sleep", lambda _seconds: None)

    def blocked(src: str, dst: str) -> None:
        exc = PermissionError("access denied")
        exc.winerror = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(platform_paths.os, "replace", blocked)
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")

    with pytest.raises(platform_paths.ReplaceBlockedError) as raised:
        platform_paths.atomic_write_text(path, "new")

    assert raised.value.reason == "replace_blocked_by_open_handle"
    assert "replace_blocked_by_open_handle" in str(raised.value)
    assert path.read_text(encoding="utf-8") == "old"
    assert _staged(tmp_path) == []


def test_atomic_write_text_creates_the_file_with_the_requested_mode(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"

    platform_paths.atomic_write_text(path, "{}", file_mode=0o600)

    assert path.read_text(encoding="utf-8") == "{}"
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
