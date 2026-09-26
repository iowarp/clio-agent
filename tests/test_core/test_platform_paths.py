"""Pure unit tests for the win32 extended-length path helper (runs on every OS).

:func:`clio_agent.platform_paths.win_extended_path` is gated on ``sys.platform``,
so these tests monkeypatch ``sys.platform`` to exercise the win32 string logic
deterministically on Linux CI -- the logic itself is pure string manipulation,
only the activation is platform-gated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clio_agent import platform_paths


def test_win_extended_path_is_a_no_op_off_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    assert platform_paths.win_extended_path(r"C:\Users\jaime\foo") == r"C:\Users\jaime\foo"


def test_win_extended_path_is_a_no_op_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    assert platform_paths.win_extended_path("/Users/jaime/foo") == "/Users/jaime/foo"


def test_win_extended_path_prefixes_a_drive_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    result = platform_paths.win_extended_path(r"C:\Users\jaime\deep\workspace\file.txt")

    assert result == "\\\\?\\C:\\Users\\jaime\\deep\\workspace\\file.txt"


def test_win_extended_path_prefixes_a_unc_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    result = platform_paths.win_extended_path(r"\\server\share\folder\file.txt")

    assert result == "\\\\?\\UNC\\server\\share\\folder\\file.txt"


def test_win_extended_path_normalizes_forward_slashes_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    result = platform_paths.win_extended_path("C:/Users/jaime/foo")

    assert result == "\\\\?\\C:\\Users\\jaime\\foo"


def test_win_extended_path_is_idempotent_on_an_already_extended_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    already = "\\\\?\\C:\\Users\\jaime\\foo"

    assert platform_paths.win_extended_path(already) == already


def test_win_extended_path_leaves_a_relative_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert platform_paths.win_extended_path("relative/path") == "relative/path"


def test_win_extended_path_leaves_a_drive_relative_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``C:foo`` (drive-relative, no root) cannot be extended -- the ``\\?\\``
    prefix requires a fully qualified path; extending it would silently change
    its meaning rather than merely widen its length budget.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    assert platform_paths.win_extended_path("C:foo") == "C:foo"


def test_win_extended_path_accepts_a_path_object(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import PureWindowsPath

    monkeypatch.setattr(sys, "platform", "win32")

    result = platform_paths.win_extended_path(PureWindowsPath(r"C:\Users\jaime\foo"))

    assert result == "\\\\?\\C:\\Users\\jaime\\foo"


def test_short_stage_name_is_short_and_unique() -> None:
    names = {platform_paths.short_stage_name() for _ in range(500)}

    assert len(names) == 500, "collided -- entropy source is broken"
    for name in names:
        assert len(name) <= 20
        assert name.startswith(".part-")
        assert name.endswith(".tmp")


def test_short_stage_name_never_repeats_a_target_filename() -> None:
    """The whole point: it must NOT embed the (potentially long) real filename.

    Regression guard for the exact staging shape that pushed a resource-
    materialization path past Windows' 260-character MAX_PATH (a hidden dot
    + the real filename + a full 32-hex uuid4 + ``.tmp``).
    """
    long_filename = "uploaded-1790285681072" + "x" * 200 + ".pdf"

    name = platform_paths.short_stage_name()

    assert long_filename not in name
    assert len(name) < len(long_filename)


def test_short_stage_name_honors_custom_prefix_and_suffix() -> None:
    name = platform_paths.short_stage_name(prefix="manifest", suffix=".json.tmp")

    assert name.startswith(".manifest-")
    assert name.endswith(".json.tmp")


# ---------------------------------------------------------------------------
# atomic_replace: retry-on-transient-Windows-sharing-race (overlay.py PermissionError)
# ---------------------------------------------------------------------------


def _permission_error(winerror: int | None) -> PermissionError:
    exc = PermissionError("access denied")
    if winerror is not None:
        exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


def test_atomic_replace_happy_path_replaces_file(tmp_path: Path) -> None:
    source = tmp_path / "source.tmp"
    target = tmp_path / "target.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")

    platform_paths.atomic_replace(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_atomic_replace_retries_a_transient_sharing_violation_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_paths.time, "sleep", lambda _seconds: None)
    source = tmp_path / "source.tmp"
    target = tmp_path / "target.json"
    source.write_text("new", encoding="utf-8")
    calls: list[int] = []
    real_replace = platform_paths.os.replace

    def flaky_replace(src: str, dst: str) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise _permission_error(32)  # ERROR_SHARING_VIOLATION
        real_replace(src, dst)

    monkeypatch.setattr(platform_paths.os, "replace", flaky_replace)

    platform_paths.atomic_replace(source, target)

    assert len(calls) == 3
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_replace_reraises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_paths.time, "sleep", lambda _seconds: None)
    calls: list[int] = []

    def always_fails(src: str, dst: str) -> None:
        calls.append(1)
        raise _permission_error(5)  # ERROR_ACCESS_DENIED

    monkeypatch.setattr(platform_paths.os, "replace", always_fails)

    with pytest.raises(PermissionError):
        platform_paths.atomic_replace(tmp_path / "source.tmp", tmp_path / "target.json", retries=3)

    assert len(calls) == 3


def test_atomic_replace_does_not_retry_a_non_retryable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DURABLE permission failure (no winerror, or a real ACL denial) must not
    burn the retry budget -- only the specific transient sharing race is retried."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_paths.time, "sleep", lambda _seconds: None)
    calls: list[int] = []

    def durable_failure(src: str, dst: str) -> None:
        calls.append(1)
        raise _permission_error(None)

    monkeypatch.setattr(platform_paths.os, "replace", durable_failure)

    with pytest.raises(PermissionError):
        platform_paths.atomic_replace(tmp_path / "source.tmp", tmp_path / "target.json", retries=5)

    assert len(calls) == 1


def test_atomic_replace_never_retries_off_win32(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    calls: list[int] = []

    def fails(src: str, dst: str) -> None:
        calls.append(1)
        raise _permission_error(32)

    monkeypatch.setattr(platform_paths.os, "replace", fails)

    with pytest.raises(PermissionError):
        platform_paths.atomic_replace(tmp_path / "source.tmp", tmp_path / "target.json", retries=5)

    assert len(calls) == 1
