"""Read authoritative terminal owners and actual filesystem artifacts after live turns."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


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
        with sqlite3.connect(stores[0].as_uri() + "?mode=ro", uri=True) as db:
            manifest = json.loads(
                db.execute(
                    "SELECT body FROM records WHERE kind='manifest' AND id=?", (manifest_id,)
                ).fetchone()[0]
            )
        result.update(manifest_id=manifest_id, entries=len(manifest["entries"]))
        if kind == "Indexing":
            expected = {
                p.relative_to(source["root"]).as_posix() for p in Path(source["root"]).rglob("*")
            }
            actual = {p["path"] for p in manifest["entries"]}
            result["exact_manifest_matches"] = actual == expected
            result["pass"] &= actual == expected and len(actual) == source["entries"]
        else:
            path = Path(rows[0]["result"]["source"]["local_path"]) / source["selected_path"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            size = path.stat().st_size if path.is_file() else None
            result.update(
                actual_bytes=size, actual_sha256=digest, manifest_hashes=manifest["hashes"]
            )
            result["pass"] &= (
                size == source["bytes"]
                and digest == source["sha256"]
                and manifest["hashes"].get(source["selected_path"]) == digest
            )
            upstream = Path(source["root"])
            expected = {
                p.relative_to(upstream).as_posix()
                for p in upstream.rglob("*")
                if any(
                    p.relative_to(upstream).as_posix() == selected
                    or p.relative_to(upstream).as_posix().startswith(selected + "/")
                    for selected in source["selected_paths"]
                )
            }
            actual = {p.relative_to(path.parent).as_posix() for p in path.parent.rglob("*")}
            expected_hashes = {
                name: hashlib.sha256((upstream / name).read_bytes()).hexdigest()
                for name in expected
                if (upstream / name).is_file()
            }
            actual_hashes = {
                name: hashlib.sha256((path.parent / name).read_bytes()).hexdigest()
                for name in actual
                if (path.parent / name).is_file()
            }
            result.update(
                exact_selections_match=actual == expected,
                all_file_hashes_match=actual_hashes == expected_hashes == manifest["hashes"],
                actual_file_count=len(actual_hashes),
                actual_total_bytes=sum(
                    (path.parent / name).stat().st_size for name in actual_hashes
                ),
            )
            result["pass"] &= (
                actual == expected and actual_hashes == expected_hashes == manifest["hashes"]
            )
    return result
