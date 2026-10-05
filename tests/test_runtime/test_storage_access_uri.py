"""Regression coverage for actual read-only connected-source ledger access."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path, PureWindowsPath

import pytest

from clio_agent.runtime import storage_access


@pytest.mark.parametrize(
    ("path", "uri"),
    [
        (r"D:\CLIO data\sources.sqlite3", "file:///D:/CLIO%20data/sources.sqlite3?mode=ro"),
        (r"\\?\D:\CLIO data\sources.sqlite3", "file:///D:/CLIO%20data/sources.sqlite3?mode=ro"),
        (r"\\server\share\sources.sqlite3", "file:////server/share/sources.sqlite3?mode=ro"),
        (r"\\?\UNC\server\share\sources.sqlite3", "file:////server/share/sources.sqlite3?mode=ro"),
        (r"D:\hash#query?\sources.sqlite3", "file:///D:/hash%23query%3F/sources.sqlite3?mode=ro"),
    ],
)
def test_windows_read_only_uri(path: str, uri: str) -> None:
    """Win32 device prefixes and URI delimiters must not become authorities/options."""
    assert storage_access._read_only_database_uri(PureWindowsPath(path)) == uri


def test_read_actual_ledger_without_write_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read actual records through a read-only connection, including extended Windows roots."""
    root = tmp_path / "CLIO data # evidence"
    directory = root / "connected-sources"
    directory.mkdir(parents=True)
    database = directory / "sources.sqlite3"
    record = {
        "source": {"id": "src_test", "provider": "local", "root": str(tmp_path / "dataset")},
        "connected": True,
    }
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE records (kind TEXT, body TEXT)")
        db.execute("INSERT INTO records VALUES ('source', ?)", (json.dumps(record),))
    effective_root = Path("\\\\?\\" + str(root)) if sys.platform == "win32" else root
    monkeypatch.setattr(storage_access.paths, "user_data_dir", lambda: effective_root)
    assert storage_access._source_records() == [record]
    with sqlite3.connect(
        storage_access._read_only_database_uri(
            effective_root / "connected-sources" / "sources.sqlite3"
        ),
        uri=True,
    ) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM records")


def test_corrupt_ledger_still_refuses_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path support must preserve fail-closed behavior for unreadable decisions."""
    directory = tmp_path / "connected-sources"
    directory.mkdir()
    (directory / "sources.sqlite3").write_bytes(b"not a SQLite database")
    monkeypatch.setattr(storage_access.paths, "user_data_dir", lambda: tmp_path)
    with pytest.raises(PermissionError, match="access decisions could not be read"):
        storage_access._source_records()
