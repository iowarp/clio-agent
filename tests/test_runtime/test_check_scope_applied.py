"""The access decisions that open a check scope really read the ledger once.

``check_scope`` only pays off where a decision is wrapped in it: the permission gate's
folder check (``gact.folder_access.requested_folders``) and the filesystem tools
(``tools.servers.fs_server``). These count the ledger reads one such decision makes.
``checked`` refuses a coroutine function: the scope would close when the coroutine is
created, before any of its reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import check_scope, storage_access


def _count_ledger_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reads: list[int] = []

    def read() -> list[dict[str, Any]]:
        reads.append(1)
        return []

    monkeypatch.setattr(storage_access, "_read_source_records", read)
    return reads


def test_the_gates_folder_check_reads_the_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop ``@checked`` from ``requested_folders`` and it reads it per check."""
    from clio_agent.gact.folder_access import requested_folders

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    reads = _count_ledger_reads(monkeypatch)
    assert requested_folders("fs_read_file", {"path": str(tmp_path / "notes.txt")}) == []
    assert len(reads) == 1


def test_a_filesystem_tool_reads_the_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop ``@checked`` from ``fs_server.list_directory`` and one listing reads
    it more than once."""
    from clio_agent.tools.servers.fs_server import list_directory

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    reads = _count_ledger_reads(monkeypatch)
    listing = list_directory(str(tmp_path))
    assert {entry["name"] for entry in listing["entries"]} >= {"a.txt", "b.txt"}
    assert len(reads) == 1


def test_checked_refuses_a_coroutine_function() -> None:
    """SABOTAGE: drop the coroutine check in ``checked`` and this decorates silently."""

    async def decide() -> None:
        return None

    with pytest.raises(TypeError, match="coroutine"):
        check_scope.checked(decide)
