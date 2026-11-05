"""Regression coverage for actual read-only connected-source ledger access."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

from clio_agent.runtime import storage_access


@pytest.mark.parametrize("extended_root", [False, True])
def test_windows_namespace_aliases_do_not_bypass_source_protection(
    monkeypatch: pytest.MonkeyPatch, extended_root: bool
) -> None:
    """Credential denial and source read-only decisions survive either path spelling."""
    root_prefix = "\\\\?\\" if extended_root else ""
    candidate_prefix = "" if extended_root else "\\\\?\\"
    monkeypatch.setattr(
        storage_access,
        "storage_access_roots",
        lambda: (
            (PureWindowsPath(root_prefix + r"D:\source"),),
            (PureWindowsPath(root_prefix + r"D:\config\storage-auth"),),
        ),
    )

    class ResolvedAlias:
        def __init__(self, suffix: str) -> None:
            self.value = PureWindowsPath(candidate_prefix + suffix)

        def resolve(self, strict: bool = False) -> PureWindowsPath:
            return self.value

    source = cast(Path, ResolvedAlias(r"D:\source\observations.csv"))
    storage_access.check_storage_access(source)
    with pytest.raises(PermissionError, match="read-only"):
        storage_access.check_storage_access(source, write=True)
    with pytest.raises(PermissionError, match="private"):
        storage_access.check_storage_access(
            cast(Path, ResolvedAlias(r"D:\config\storage-auth\credentials.json"))
        )
    storage_access.check_storage_access(
        cast(Path, ResolvedAlias(r"D:\source-other\output.csv")), write=True
    )


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


def test_vault_is_read_only_and_private_folders_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project persisted vault permissions into generic filesystem and child fences."""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".git").mkdir()
    directory = tmp_path / "data" / "connected-sources"
    directory.mkdir(parents=True)
    record = {
        "source": {"id": "vault", "provider": "obsidian", "root": str(vault)},
        "connected": True,
        "configuration": {"obsidian_excluded_folders": ["Private"]},
    }
    with sqlite3.connect(directory / "sources.sqlite3") as db:
        db.execute("CREATE TABLE records (kind TEXT, body TEXT)")
        db.execute("INSERT INTO records VALUES ('source', ?)", (json.dumps(record),))
    monkeypatch.setattr(storage_access.paths, "user_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(storage_access.paths, "user_config_dir", lambda: tmp_path / "config")
    storage_access.check_storage_access(vault / "note.md")
    with pytest.raises(PermissionError, match="read-only"):
        storage_access.check_storage_access(vault / "note.md", write=True)
    for path in (
        vault / ".obsidian" / "config.json",
        vault / ".git" / "config",
        vault / "Private" / "note.md",
    ):
        with pytest.raises(PermissionError, match="private"):
            storage_access.check_storage_access(path)


def test_the_ledger_is_read_once_and_a_commit_is_seen_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every gate check of a path reads the ledger; a script's 1,000 nested calls opened
    SQLite 2,000 times. An unchanged ledger is not reopened; a commit is read at once.

    SABOTAGE: return the cached rows without comparing ``_ledger_signature`` and the
    second source never appears; drop the cache and ``connect`` runs every time."""
    directory = tmp_path / "connected-sources"
    directory.mkdir()
    database = directory / "sources.sqlite3"
    first = {"source": {"id": "src_a"}, "connected": True}
    second = {"source": {"id": "src_b"}, "connected": True}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE records (kind TEXT, body TEXT)")
        db.execute("INSERT INTO records VALUES ('source', ?)", (json.dumps(first),))
    monkeypatch.setattr(storage_access.paths, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(storage_access, "_LEDGER", None)
    opened: list[str] = []
    real_connect = storage_access.sqlite3.connect

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(str(args[0]))
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(storage_access.sqlite3, "connect", connect)
    assert storage_access._source_records() == [first]
    assert storage_access._source_records() == [first]
    assert len(opened) == 1
    with real_connect(database) as db:
        db.execute("INSERT INTO records VALUES ('source', ?)", (json.dumps(second),))
    assert storage_access._source_records() == [first, second]
    assert len(opened) == 2
