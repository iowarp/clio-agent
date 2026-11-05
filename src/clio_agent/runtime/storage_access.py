"""Project connected-source access modes into filesystem tool and sandbox boundaries."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any
from urllib.parse import quote

from clio_agent import paths
from clio_agent.platform_paths import path_is_within


def _read_only_database_uri(database: PurePath) -> str:
    """Encode a local SQLite URI without mistaking Win32 prefixes for a host."""
    if isinstance(database, PureWindowsPath):
        text = str(database)
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
        path = text.replace("\\", "/")
        # SQLite accepts only empty/localhost URI authorities. Keep a UNC
        # server in the pathname, rather than producing file://server/....
        prefix = "file://" if path.startswith("//") else "file:///"
        return prefix + quote(path, safe="/:") + "?mode=ro"
    return database.as_uri() + "?mode=ro"


#: The last decoded ledger, keyed by its file signature. Every permission check of a
#: path argument reads the ledger (twice: the storage roots and the materialized read
#: roots), and a script's nested calls are checked one by one: opening SQLite per check
#: was a third of a script's own time per call (2026-11-05 profile, 1,000 listings).
_LEDGER: tuple[tuple[Any, ...], list[dict]] | None = None
_LEDGER_LOCK = threading.Lock()


def _ledger_signature(database: Path) -> tuple[Any, ...]:
    """What changes on every commit: SQLite's file change counter, and each file's stat."""
    parts: list[Any] = [str(database)]
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-journal")):
        try:
            stat = path.stat()
        except OSError:
            parts.append(None)
            continue
        parts.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    try:
        with database.open("rb") as handle:
            handle.seek(24)  # the header's file change counter, bumped by every commit
            parts.append(handle.read(4))
    except OSError:
        parts.append(None)
    return tuple(parts)


def _source_records() -> list[dict]:
    global _LEDGER
    database = paths.user_data_dir() / "connected-sources" / "sources.sqlite3"
    if not database.exists():
        return []
    signature = _ledger_signature(database)
    cached = _LEDGER
    if cached is not None and cached[0] == signature:
        return list(cached[1])
    try:
        db = sqlite3.connect(_read_only_database_uri(database), uri=True, timeout=5)
        try:
            rows = db.execute("SELECT body FROM records WHERE kind='source'").fetchall()
        finally:
            db.close()
        records = [json.loads(row[0]) for row in rows]
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise PermissionError("Connected-source access decisions could not be read") from exc
    if _ledger_signature(database) == signature:  # unchanged while it was read
        with _LEDGER_LOCK:
            _LEDGER = (signature, records)
    return list(records)


def materialized_read_roots(workspace_root: Path) -> tuple[Path, ...]:
    """Grant read access to approved copies for this workspace without granting writes."""
    roots = []
    for row in _source_records():
        source = row["source"]
        if (
            row.get("workspace_root")
            and Path(row["workspace_root"]).resolve() == workspace_root.resolve()
            and source.get("local_path")
            and source["materialization"] in {"ready", "stale", "transferring"}
        ):
            roots.append(Path(source["local_path"]).resolve())
            if row.get("manifest_id"):
                roots.append(
                    (
                        paths.user_data_dir()
                        / "connected-sources"
                        / source["id"]
                        / row["manifest_id"]
                    ).resolve()
                )
    return tuple(roots)


def storage_access_roots() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return read-only source/baseline roots and private credential roots.

    Read the durable owner ledger so separately spawned filesystem servers see
    the same decisions. An unreadable ledger refuses access instead of silently
    dropping a read-only decision.
    """
    try:
        private = paths.user_config_dir() / "storage-auth"
        data = paths.user_data_dir() / "connected-sources"
    except paths.HomeDirectoryUnavailable:
        return (), ()
    denied = [private.resolve()] if private.exists() else []
    headless = (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "obsidian-headless"
        if sys.platform == "linux"
        else Path.home() / ".obsidian-headless"
    )
    if headless.exists():
        denied.append(headless.resolve())
    database = data / "sources.sqlite3"
    if not database.exists():
        return (), tuple(denied)
    read_only = [data.resolve()]
    try:
        for record in _source_records():
            source = record["source"]
            if record["connected"] and source["provider"] in {"local", "obsidian"}:
                read_only.append(Path(source["root"]).resolve())
                if source["provider"] == "obsidian":
                    vault = Path(source["root"])
                    denied.append((vault / ".obsidian").resolve())
                    denied.extend(
                        (vault / folder).resolve()
                        for folder in record.get("configuration", {}).get(
                            "obsidian_excluded_folders", []
                        )
                    )
                    if vault.is_dir():
                        denied.extend(
                            child.resolve()
                            for child in vault.iterdir()
                            if child.name.startswith(".")
                        )
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise PermissionError("Connected-source access decisions could not be read") from exc
    return tuple(read_only), tuple(denied)


def check_storage_access(path: Path, *, write: bool = False) -> None:
    """Keep source writes on the explicit review path and credentials out of tools."""
    read_only, denied = storage_access_roots()
    resolved = path.resolve(strict=False)
    if any(path_is_within(resolved, root) for root in denied):
        raise PermissionError(
            "Storage sign-in credentials are private to the trusted setup process"
        )
    if write and any(
        path_is_within(resolved, root) or path_is_within(root, resolved) for root in read_only
    ):
        raise PermissionError(
            "This source is read-only outside its approved linked filesystem; use that filesystem to edit it"
        )


def require_storage_fence(mechanism: str, active: bool, write_roots: tuple[Path, ...]) -> None:
    """Fail closed when a child cannot enforce protected data or credential exclusions.

    Landlock's additive write grants cannot subtract a protected descendant from
    an already writable ancestor. A tool-level check alone must not be presented
    as protection against shell commands or arbitrary MCP child code.
    """
    if active and mechanism == "codex":
        return
    read_only, denied = storage_access_roots()
    has_credentials = any(
        (root / "credentials.json").exists() or (root / "auth_token").exists() for root in denied
    )
    protected = bool(_source_records()) and (
        not active
        or any(
            protected.is_relative_to(grant.resolve()) or grant.resolve().is_relative_to(protected)
            for grant in write_roots
            for protected in read_only
        )
    )
    if has_credentials or protected:
        raise PermissionError(
            "This host's child-process sandbox cannot enforce connected-source exclusions. "
            "Open Infrastructure > CLIO hosts > Protected execution and select "
            "Set up protected execution, then retry. For a headless installation, "
            "run `clio sandbox setup`. Shell and MCP tools remain blocked until verification succeeds. "
            "Bypass checks skips action approvals; it does not disable source-access protection."
        )
