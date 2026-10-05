"""Project connected-source access modes into filesystem tool and sandbox boundaries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path, PurePath, PureWindowsPath
from urllib.parse import quote

from clio_agent import paths


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


def _source_records() -> list[dict]:
    database = paths.user_data_dir() / "connected-sources" / "sources.sqlite3"
    if not database.exists():
        return []
    try:
        db = sqlite3.connect(_read_only_database_uri(database), uri=True, timeout=5)
        try:
            rows = db.execute("SELECT body FROM records WHERE kind='source'").fetchall()
        finally:
            db.close()
        return [json.loads(row[0]) for row in rows]
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise PermissionError("Connected-source access decisions could not be read") from exc


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
    denied = (private.resolve(),) if private.exists() else ()
    database = data / "sources.sqlite3"
    if not database.exists():
        return (), denied
    read_only = [data.resolve()]
    try:
        for record in _source_records():
            source = record["source"]
            if record["connected"] and source["provider"] == "local":
                read_only.append(Path(source["root"]).resolve())
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise PermissionError("Connected-source access decisions could not be read") from exc
    return tuple(read_only), denied


def check_storage_access(path: Path, *, write: bool = False) -> None:
    """Keep source writes on the explicit review path and credentials out of tools."""
    read_only, denied = storage_access_roots()
    resolved = path.resolve(strict=False)
    if any(resolved.is_relative_to(root) for root in denied):
        raise PermissionError(
            "Storage sign-in credentials are private to the trusted setup process"
        )
    if write and any(
        resolved.is_relative_to(root) or root.is_relative_to(resolved) for root in read_only
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
    has_credentials = any((root / "credentials.json").exists() for root in denied)
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
            "Enable the supported Codex sandbox before using connected data with agent tools."
        )
