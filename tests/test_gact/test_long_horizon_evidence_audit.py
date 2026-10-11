"""A qualification cannot pass from files alone or a model's completion claim."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def audit() -> ModuleType:
    """Load only the independent standard-library verifier."""
    path = (
        Path(__file__).resolve().parents[2] / "scripts/live_verification/long_horizon_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("evidence_audit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_run(root: Path, count: int = 2) -> tuple[list[dict[str, Any]], dict[str, str], Path]:
    """Build timestamped observation fixtures; no product runtime is replaced."""
    workspace = root / "runtime/workspace"
    workspace.mkdir(parents=True)
    marker = "CLIO-LONG-HORIZON-20261011"
    rows: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    observed: dict[str, float] = {}
    base = time.time() - (count - 1) * 3604 - 100

    def stamp(at: float) -> str:
        return datetime.fromtimestamp(at, timezone.utc).isoformat()

    def create(name: str, raw: bytes, cid: str) -> dict[str, Any]:
        return {
            "name": "create_artifact",
            "call_id": cid,
            "args": {"name": name},
            "ok": True,
            "cached": False,
            "telemetry_source": "live_observer",
            "result": {
                "created": 1,
                "deduplicated": 0,
                "rejected": 0,
                "artifacts": [
                    {
                        "accepted": True,
                        "created": True,
                        "version": 1,
                        "name": name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
            },
        }

    for number in range(count):
        at = base + number * 3604
        name = f"batch-{number:03d}.json"
        raw = json.dumps(
            {"batch": number, "next": number + 1, "previous": number - 1, "marker": marker}
        ).encode()
        path = workspace / name
        path.write_bytes(raw)
        os.utime(path, (at + 2, at + 2))
        hashes[name] = hashlib.sha256(raw).hexdigest()
        observed[name] = at + 20
        calls: list[dict[str, Any]] = []
        if number:
            previous = workspace / f"batch-{number - 1:03d}.json"
            calls.append(
                {
                    "name": "fs_read_file",
                    "call_id": f"read-{number}",
                    "args": {"filepath": str(previous)},
                    "ok": True,
                    "cached": False,
                    "telemetry_source": "live_observer",
                    "result": json.dumps(
                        {"path": str(previous), "content": previous.read_text(), "truncated": False}
                    ),
                }
            )
        calls.append(create(name, raw, f"create-{number}"))
        if number == 48:
            manifest = json.dumps({"marker": marker, "files": sorted(hashes)}).encode()
            (workspace / "final-manifest.json").write_bytes(manifest)
            calls.append(create("final-manifest.json", manifest, "manifest"))
        else:
            calls.append(
                {
                    "name": "loop_wakeup",
                    "call_id": f"wake-{number}",
                    "args": {"delay_seconds": 3600, "stop": False},
                    "ok": True,
                    "cached": False,
                    "telemetry_source": "live_observer",
                    "result": {"next_fire_at": stamp(at + 3604)},
                }
            )
        parts = [
            {
                "type": "tool_result",
                "call_id": c["call_id"],
                "metadata": {
                    "completed_at": stamp(at + (2 if c["name"] == "create_artifact" else 4))
                },
            }
            for c in calls
        ]
        rows.append(
            {
                "id": f"user-{number}",
                "role": "user",
                "created_at": stamp(at),
                "metadata": {"scheduled": True, "schedule_id": f"schedule-{number}"}
                if number
                else {},
            }
        )
        rows.append(
            {
                "id": f"answer-{number}",
                "role": "assistant",
                "turn_id": f"user-{number}",
                "created_at": stamp(at + 5),
                "metadata": {"tools_called": calls},
                "parts": parts,
            }
        )
    (root / "identity.json").write_text(json.dumps({"started": base}))
    (root / "file-evidence.json").write_text(
        json.dumps({"sha256": hashes, "observed_at": observed})
    )
    (root / "transcript.json").write_text(json.dumps({"messages": rows}))
    status = {
        "status": "running",
        "session_status": "idle",
        "elapsed_monotonic_seconds": (count - 1) * 3604 + 100,
        "goal": {
            "active": count < 49,
            "cleared": count == 49,
            "met": count == 49,
            "clear_reason": "goal_met" if count == 49 else "",
        },
        "loop": {
            "active": count < 49,
            "stopped": count == 49,
            "stop_reason": "loop_goal_met" if count == 49 else "",
            "pending_schedule_id": "next",
        },
        "schedules": [{"id": "next"}] if count < 49 else [],
    }
    (root / "status.json").write_text(json.dumps(status))
    return rows, hashes, workspace


def test_read_only_goal_status_does_not_substitute_checkpoint_work(
    audit: ModuleType, tmp_path: Path
) -> None:
    """Control inspection is valid, while every real read/write remains mandatory."""
    rows, hashes, workspace = build_run(tmp_path)
    status = {
        "call_id": "status-query",
        "name": "goal_status",
        "args": {},
        "ok": True,
        "cached": False,
        "telemetry_source": "live_observer",
        "result": {"active": True},
    }
    rows[1]["metadata"]["tools_called"].insert(0, status)
    assert audit.verify_transcript(rows, hashes, workspace)["checkpoints"] == 2
    rows[3]["metadata"]["tools_called"][0] = status
    with pytest.raises(AssertionError, match="read once and write once"):
        audit.verify_transcript(rows, hashes, workspace)


def test_partial_can_never_claim_full_qualification(audit: ModuleType, tmp_path: Path) -> None:
    build_run(tmp_path)
    result = audit.audit_record(tmp_path)
    assert result["qualified"] is False and result["scheduled_turns"] == 1
    with pytest.raises(AssertionError, match="49 checkpoints"):
        audit.audit_record(tmp_path, complete=True)


def test_complete_requires_all_independent_evidence(audit: ModuleType, tmp_path: Path) -> None:
    build_run(tmp_path, 49)
    result = audit.audit_record(tmp_path, complete=True)
    assert result["qualified"] is True and result["scheduled_turns"] == 48


def test_replacing_hourly_wakeup_is_not_a_duplicate_file_effect(
    audit: ModuleType, tmp_path: Path
) -> None:
    """A permitted same-turn control revision must not be confused with another write."""
    rows, hashes, workspace = build_run(tmp_path)
    answer = rows[3]
    early = copy.deepcopy(answer["metadata"]["tools_called"][-1])
    early["call_id"] = "early-wake"
    answer["metadata"]["tools_called"].insert(0, early)
    part = copy.deepcopy(answer["parts"][-1])
    part["call_id"] = "early-wake"
    answer["parts"].insert(0, part)
    assert audit.verify_transcript(rows, hashes, workspace)["scheduled_turns"] == 1


@pytest.mark.parametrize(
    "defect",
    [
        "missing_read",
        "cached_read",
        "failed_read",
        "invented_observer",
        "wrong_read_bytes",
        "wrong_read_path",
        "duplicate_write",
        "wrong_write_hash",
        "deduplicated_write",
        "manual_continuation",
        "duplicate_schedule",
        "short_wakeup",
        "early_delivery",
        "wrong_turn",
        "unexpected_shell",
    ],
)
def test_transcript_rejects_false_evidence(audit: ModuleType, tmp_path: Path, defect: str) -> None:
    rows, hashes, workspace = build_run(tmp_path, 3)
    rows = copy.deepcopy(rows)
    calls = rows[3]["metadata"]["tools_called"]
    if defect == "missing_read":
        calls.pop(0)
    elif defect == "cached_read":
        calls[0]["cached"] = True
    elif defect == "failed_read":
        calls[0]["ok"] = False
    elif defect == "invented_observer":
        calls[0]["telemetry_source"] = "model_claim"
    elif defect in {"wrong_read_bytes", "wrong_read_path"}:
        value = json.loads(calls[0]["result"])
        value["content" if defect == "wrong_read_bytes" else "path"] = "incorrect"
        calls[0]["result"] = json.dumps(value)
    elif defect == "duplicate_write":
        calls.insert(2, copy.deepcopy(calls[1]))
    elif defect == "wrong_write_hash":
        calls[1]["result"]["artifacts"][0]["sha256"] = "invented"
    elif defect == "deduplicated_write":
        calls[1]["result"]["deduplicated"] = 1
    elif defect == "manual_continuation":
        rows[2]["metadata"] = {}
    elif defect == "duplicate_schedule":
        rows[4]["metadata"]["schedule_id"] = "schedule-1"
    elif defect == "short_wakeup":
        calls[-1]["args"]["delay_seconds"] = 60
    elif defect == "early_delivery":
        rows[2]["created_at"] = rows[0]["created_at"]
    elif defect == "wrong_turn":
        rows[3]["turn_id"] = "another-turn"
    elif defect == "unexpected_shell":
        calls[0]["name"] = "shell_bash"
    with pytest.raises(AssertionError):
        audit.verify_transcript(rows, hashes, workspace)


@pytest.mark.parametrize(
    "defect",
    ["same_bytes_rewrite", "lost_wakeup", "stale_status", "pending_turn", "premature_time"],
)
def test_complete_or_progress_rejects_invalid_state(
    audit: ModuleType, tmp_path: Path, defect: str
) -> None:
    _, _, workspace = build_run(tmp_path, 49 if defect in {"pending_turn", "premature_time"} else 2)
    status_path = tmp_path / "status.json"
    if defect == "same_bytes_rewrite":
        path = workspace / "batch-000.json"
        path.write_bytes(path.read_bytes())
    elif defect == "stale_status":
        os.utime(status_path, (time.time() - 120, time.time() - 120))
    elif defect == "premature_time":
        (tmp_path / "identity.json").write_text(json.dumps({"started": time.time()}))
    else:
        status = json.loads(status_path.read_text())
        if defect == "lost_wakeup":
            status["schedules"] = []
        else:
            status["session_status"] = "running"
        status_path.write_text(json.dumps(status))
    with pytest.raises(AssertionError):
        audit.audit_record(tmp_path, complete=defect in {"pending_turn", "premature_time"})


@pytest.mark.parametrize(
    "defect",
    [
        "active_goal",
        "not_met",
        "uncleared_goal",
        "unfinished_loop",
        "pending_wake",
        "wrong_manifest",
        "short_monotonic",
        "missing_monotonic",
    ],
)
def test_final_files_alone_cannot_pass(audit: ModuleType, tmp_path: Path, defect: str) -> None:
    """Forty-nine files still need an unambiguous completed goal and settled loop."""
    build_run(tmp_path, 49)
    status_path = tmp_path / "status.json"
    status = json.loads(status_path.read_text())
    if defect == "active_goal":
        status["goal"]["active"] = True
    elif defect == "not_met":
        status["goal"]["met"] = False
    elif defect == "uncleared_goal":
        status["goal"]["cleared"] = False
    elif defect == "unfinished_loop":
        status["loop"]["stopped"] = False
    elif defect == "pending_wake":
        status["schedules"] = [{"id": "orphan"}]
    elif defect == "short_monotonic":
        status["elapsed_monotonic_seconds"] = 60
    elif defect == "missing_monotonic":
        status.pop("elapsed_monotonic_seconds")
    else:
        manifest = tmp_path / "runtime/workspace/final-manifest.json"
        manifest.write_text(json.dumps({"marker": "wrong", "files": []}))
    status_path.write_text(json.dumps(status))
    with pytest.raises(AssertionError):
        audit.audit_record(tmp_path, complete=True)
