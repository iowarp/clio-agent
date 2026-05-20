"""Tests for filesystem edit tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.tools.file_policy import FilePolicyError
from clio_agent.tools.fs_write import write_text_with_policy
from clio_agent.tools.servers.fs_server import apply_edit_write, propose_edit


def test_propose_edit_allows_new_file_under_write_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "new.txt"

    result = propose_edit(str(target), "hello\n")

    assert result["path"] == str(target.resolve())
    assert result["lines_added"] == 1
    assert result["lines_removed"] == 0
    assert "hello" in result["unified_diff"]
    assert not target.exists()


def test_apply_edit_write_allows_new_file_under_write_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "new.txt"

    result = apply_edit_write(str(target), "hello\n")

    assert result["ok"] is True
    assert result["path"] == str(target.resolve())
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_apply_edit_write_uses_shared_policy_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "shared.txt"

    direct = write_text_with_policy(str(target), "first\n")
    via_tool = apply_edit_write(str(target), "second\n")

    assert direct["path"] == via_tool["path"]
    assert via_tool["ok"] is True
    assert target.read_text(encoding="utf-8") == "second\n"


def test_apply_edit_write_rejects_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(allowed))

    with pytest.raises(FilePolicyError) as exc:
        apply_edit_write(str(outside / "new.txt"), "nope\n")

    assert exc.value.code == "outside_allowed_roots"
    assert not (outside / "new.txt").exists()
