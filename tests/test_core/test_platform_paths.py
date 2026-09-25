"""Pure unit tests for the win32 extended-length path helper (runs on every OS).

:func:`clio_agent.platform_paths.win_extended_path` is gated on ``sys.platform``,
so these tests monkeypatch ``sys.platform`` to exercise the win32 string logic
deterministically on Linux CI -- the logic itself is pure string manipulation,
only the activation is platform-gated.
"""

from __future__ import annotations

import sys

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
