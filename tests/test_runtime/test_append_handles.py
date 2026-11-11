"""Cached append handles: one open per path, reopened after rotation, bounded, optional.

Reopening a log per line cost about a tenth of a busy server's active time in the
stream audit and in the semantic trace writer (P7, 2026-11-02).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.runtime import append_handles
from clio_agent.runtime.append_handles import AppendHandles


def test_lines_reuse_one_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: drop the cached handle after every write and two opens are counted."""
    opened: list[str] = []
    original = Path.open

    def counting(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        opened.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(append_handles.Path, "open", counting)
    handles = AppendHandles(keep_open=True)
    log = tmp_path / "deep" / "a.jsonl"
    handles.write(log, "one")
    handles.write(log, "two")
    handles.close()
    assert opened == ["a.jsonl"]  # before read_text, which opens it too
    assert log.read_text(encoding="utf-8") == "one\ntwo\n"


def test_the_least_recent_handle_is_closed_past_the_bound(tmp_path: Path) -> None:
    handles = AppendHandles(max_open=2, keep_open=True)
    for name in ("a", "b", "c"):
        handles.write(tmp_path / f"{name}.jsonl", name)
    assert list(handles._open) == [str(tmp_path / "b.jsonl"), str(tmp_path / "c.jsonl")]
    handles.close()


def test_without_keep_open_no_handle_stays_open(tmp_path: Path) -> None:
    handles = AppendHandles(keep_open=False)
    handles.write(tmp_path / "a.jsonl", "one")
    assert handles._open == {}
    assert (tmp_path / "a.jsonl").read_text(encoding="utf-8") == "one\n"
