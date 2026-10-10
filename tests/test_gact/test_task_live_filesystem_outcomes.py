"""Regression coverage for actual-task filesystem qualification evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from clio_agent.platform_paths import win_extended_path


@pytest.mark.parametrize("corrupt_nested", [False, True])
def test_download_outcome_audits_long_windows_paths(tmp_path: Path, corrupt_nested: bool) -> None:
    """Count and hash every selected real file beyond the legacy Windows path limit."""
    proof = tmp_path / "proof"
    upstream = proof / "upstream"
    materialized = proof / "materialized"
    while len(str(materialized / "group" / "nested.txt")) < 280:
        materialized /= "selected-snapshot"
    payload = b"owned payload\n"
    nested = b"owned nested file\n"
    for root in (upstream, materialized):
        os.makedirs(win_extended_path(root / "group"), exist_ok=True)
        for name, content in (("payload.bin", payload), ("group/nested.txt", nested)):
            with open(win_extended_path(root / name), "wb") as stream:
                stream.write(
                    b"corrupted nested content\n"
                    if corrupt_nested and root == materialized and name == "group/nested.txt"
                    else content
                )
    hashes = {
        "payload.bin": hashlib.sha256(payload).hexdigest(),
        "group/nested.txt": hashlib.sha256(nested).hexdigest(),
    }
    manifest_id = "owned-long-path-manifest"
    store = proof / "clio-home" / "sources.sqlite3"
    store.parent.mkdir()
    with closing(sqlite3.connect(store)) as db:
        db.execute("CREATE TABLE records (kind TEXT, id TEXT, body TEXT)")
        db.execute(
            "INSERT INTO records VALUES (?, ?, ?)",
            (
                "manifest",
                manifest_id,
                json.dumps(
                    {
                        "entries": [{"path": path} for path in (*hashes, "group")],
                        "hashes": hashes,
                    }
                ),
            ),
        )
        db.commit()
    helper = Path(__file__).parents[2] / "scripts/live_verification/_task_outcomes.py"
    spec = importlib.util.spec_from_file_location("task_live_outcomes", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.outcomes(
        proof,
        "Download",
        {
            "processes": [
                {
                    "handle": "owned-task",
                    "effective_status": "completed",
                    "result": {
                        "manifest_id": manifest_id,
                        "source": {"local_path": str(materialized)},
                    },
                }
            ]
        },
        {"accepted": [{"handle": "owned-task"}]},
        {
            "root": str(upstream),
            "selected_path": "payload.bin",
            "selected_paths": ["payload.bin", "group"],
            "bytes": len(payload),
            "sha256": hashes["payload.bin"],
        },
    )
    assert result["pass"] is (not corrupt_nested)
    assert result["exact_selections_match"] is True
    assert result["all_file_hashes_match"] is (not corrupt_nested)
    assert result["actual_file_count"] == 2
    assert result["actual_total_bytes"] == len(payload) + len(
        b"corrupted nested content\n" if corrupt_nested else nested
    )
