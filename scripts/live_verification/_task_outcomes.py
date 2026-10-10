"""Read authoritative terminal owners and actual filesystem artifacts after live turns."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, NoReturn

from clio_agent.platform_paths import win_extended_path


def _walk_error(error: OSError) -> NoReturn:
    """Fail qualification explicitly when any retained directory cannot be read."""
    raise error


def _tree_entries(root: Path) -> Iterator[tuple[str, str, os.stat_result]]:
    """Inspect the same tree through OS paths without following reparse points."""
    extended = win_extended_path(root)
    for directory, directories, files in os.walk(extended, onerror=_walk_error, followlinks=False):
        for name in [*directories, *files]:
            full = os.path.join(directory, name)
            info = os.stat(full, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Qualification custody tree contains a link: {full}")
            yield os.path.relpath(full, extended).replace("\\", "/"), full, info


def _tree_custody(root: Path) -> tuple[set[str], dict[str, str], int]:
    """Read and hash every regular file, including Windows paths beyond MAX_PATH."""
    entries: set[str] = set()
    hashes: dict[str, str] = {}
    size = 0
    for relative, full, info in _tree_entries(root):
        entries.add(relative)
        if stat.S_ISREG(info.st_mode):
            with open(full, "rb") as stream:
                hashes[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
            size += info.st_size
    return entries, hashes, size


def outcomes(
    proof: Path,
    kind: str,
    tasks: dict[str, Any],
    evidence: dict[str, Any],
    source: dict[str, Any] | None,
) -> dict[str, Any]:
    """Require successful stored settlement plus each workload's real output custody."""
    handles = {r["handle"] for r in evidence["accepted"]}
    rows = [r for r in tasks["processes"] if r["handle"] in handles]
    result: dict[str, Any] = {
        "terminal": [{"handle": r["handle"], "status": r["effective_status"]} for r in rows],
        "pass": bool(rows) and all(r["effective_status"] == "completed" for r in rows),
    }
    if kind in {"Shell", "Subagent"}:
        path = proof / "workspace" / "shell-done.txt"
        result["filesystem_output"] = path.read_text() if path.is_file() else None
        result["pass"] &= result["filesystem_output"] == "owned shell completed"
    elif kind in {"Indexing", "Download"} and source is not None:
        stores = list((proof / "clio-home").rglob("sources.sqlite3"))
        if len(stores) != 1 or len(rows) != 1:
            return {**result, "pass": False, "error": "Expected one owned store and task"}
        manifest_id = rows[0]["result"]["manifest_id"]
        with closing(sqlite3.connect(stores[0].as_uri() + "?mode=ro", uri=True)) as db:
            manifest = json.loads(
                db.execute(
                    "SELECT body FROM records WHERE kind='manifest' AND id=?", (manifest_id,)
                ).fetchone()[0]
            )
        result.update(manifest_id=manifest_id, entries=len(manifest["entries"]))
        if kind == "Indexing":
            expected = {relative for relative, _, _ in _tree_entries(Path(source["root"]))}
            actual = {p["path"] for p in manifest["entries"]}
            result["exact_manifest_matches"] = actual == expected
            result["pass"] &= actual == expected and len(actual) == source["entries"]
        else:
            path = Path(rows[0]["result"]["source"]["local_path"]) / source["selected_path"]
            native_path = win_extended_path(path)
            digest = None
            size = None
            if os.path.isfile(native_path):
                with open(native_path, "rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                size = os.stat(native_path).st_size
            result.update(
                actual_bytes=size, actual_sha256=digest, manifest_hashes=manifest["hashes"]
            )
            result["pass"] &= (
                size == source["bytes"]
                and digest == source["sha256"]
                and manifest["hashes"].get(source["selected_path"]) == digest
            )
            upstream = Path(source["root"])
            upstream_entries, upstream_hashes, _ = _tree_custody(upstream)
            expected = {
                name
                for name in upstream_entries
                if any(
                    name == selected or name.startswith(selected + "/")
                    for selected in source["selected_paths"]
                )
            }
            actual, actual_hashes, actual_size = _tree_custody(path.parent)
            expected_hashes = {
                name: digest for name, digest in upstream_hashes.items() if name in expected
            }
            result.update(
                exact_selections_match=actual == expected,
                all_file_hashes_match=actual_hashes == expected_hashes == manifest["hashes"],
                actual_file_count=len(actual_hashes),
                actual_total_bytes=actual_size,
            )
            result["pass"] &= (
                actual == expected and actual_hashes == expected_hashes == manifest["hashes"]
            )
    return result
