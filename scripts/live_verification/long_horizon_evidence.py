"""Independently audit real hourly-soak transcripts, file bytes and control state.

Read-only toward the product service: this verifier cannot send a turn, alter a
schedule, advance a clock or restart the running soak.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

COUNT = 49
MARKER = "CLIO-LONG-HORIZON-20261011"
INTERVAL = 3600


def epoch(value: str) -> float:
    """Require an aware recorded timestamp, rather than infer a local timezone."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise AssertionError("Evidence timestamp has no timezone")
    return parsed.timestamp()


def result(call: dict[str, Any]) -> dict[str, Any]:
    """Decode the real tool observer's structured or JSON-encoded result."""
    value = call.get("result")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise AssertionError("Missing actual tool result")
    return value


def verify_transcript(
    rows: list[dict[str, Any]], hashes: dict[str, str], workspace: Path
) -> dict[str, Any]:
    """Require one original request, then scheduled turns with fresh reads and writes."""
    users = sorted((m for m in rows if m["role"] == "user"), key=lambda m: epoch(m["created_at"]))
    answers = sorted(
        (m for m in rows if m["role"] == "assistant" and m.get("metadata", {}).get("tools_called")),
        key=lambda m: epoch(m["created_at"]),
    )
    if len(users) != len(hashes) or len(answers) != len(hashes):
        raise AssertionError(
            "Each checkpoint needs exactly one user turn and real tool-bearing answer"
        )
    schedules: set[str] = set()
    writes: list[float] = []
    next_due = 0.0
    for number, (user, answer) in enumerate(zip(users, answers, strict=True)):
        if answer["turn_id"] != user["id"]:
            raise AssertionError("Checkpoint answer belongs to a different turn")
        metadata = user.get("metadata") or {}
        scheduled = bool(metadata.get("scheduled"))
        if scheduled != (number > 0):
            raise AssertionError("Unexpected manual continuation or scheduled initial request")
        if number:
            schedule_id = metadata.get("schedule_id", "")
            if not schedule_id or schedule_id in schedules:
                raise AssertionError("Missing or repeatedly delivered schedule identity")
            schedules.add(schedule_id)
            if epoch(user["created_at"]) < next_due:
                raise AssertionError("Scheduled continuation arrived before its actual wakeup")
        calls = answer["metadata"]["tools_called"]
        if any(
            c.get("ok") is not True
            or c.get("cached") is not False
            or c.get("telemetry_source") != "live_observer"
            or c.get("name")
            not in {"fs_read_file", "create_artifact", "loop_wakeup", "goal_status"}
            for c in calls
        ):
            raise AssertionError("Failed, cached, unobserved or unexpected tool call")
        work = [c for c in calls if c["name"] not in {"loop_wakeup", "goal_status"}]
        wakes = [c for c in calls if c["name"] == "loop_wakeup"]
        names = [c["name"] for c in work]
        expected = (["fs_read_file"] if number else []) + ["create_artifact"]
        if number == COUNT - 1:
            expected += ["create_artifact"]
        if names != expected or (bool(wakes) != (number < COUNT - 1)):
            raise AssertionError("Checkpoint must read once and write once, with an hourly wakeup")
        if number:
            previous = f"batch-{number - 1:03d}.json"
            read = work[0]
            observed = result(read)
            if (
                Path(read["args"]["filepath"]).resolve() != (workspace / previous).resolve()
                or Path(observed.get("path", "")).resolve() != (workspace / previous).resolve()
                or observed.get("truncated") is not False
                or hashlib.sha256(observed.get("content", "").encode()).hexdigest()
                != hashes[previous]
            ):
                raise AssertionError(
                    "Previous checkpoint was not actually read with matching bytes"
                )
        file = f"batch-{number:03d}.json"
        create = work[1 if number else 0]
        _verify_created(create, file, hashes[file])
        parts = {p.get("call_id"): p for p in answer["parts"] if p["type"] == "tool_result"}
        created = epoch(parts[create["call_id"]]["metadata"]["completed_at"])
        if writes and created - writes[-1] < INTERVAL:
            raise AssertionError("Checkpoint writes were less than one real hour apart")
        writes.append(created)
        if number < COUNT - 1:
            for wake in wakes:
                if (
                    wake["args"].get("delay_seconds") != INTERVAL
                    or wake["args"].get("stop")
                    or result(wake).get("stopped") is True
                ):
                    raise AssertionError("The hourly wakeup was shortened or stopped")
                next_due = epoch(result(wake)["next_fire_at"])
                completed = epoch(parts[wake["call_id"]]["metadata"]["completed_at"])
                if next_due < completed + INTERVAL - 1:
                    raise AssertionError(
                        "The persisted wakeup does not preserve its requested delay"
                    )
            if calls[-1]["name"] != "loop_wakeup" or completed < created:
                raise AssertionError(
                    "The final hourly wakeup must follow the actual checkpoint write"
                )
        else:
            path = workspace / "final-manifest.json"
            _verify_created(work[-1], path.name, hashlib.sha256(path.read_bytes()).hexdigest())
    return {"checkpoints": len(writes), "scheduled_turns": len(schedules), "write_times": writes}


def _verify_created(call: dict[str, Any], name: str, digest: str) -> None:
    observed = result(call)
    artifacts = observed.get("artifacts", [])
    if (
        call["args"].get("name") != name
        or observed.get("created") != 1
        or observed.get("deduplicated") != 0
        or observed.get("rejected") != 0
        or len(artifacts) != 1
        or artifacts[0].get("accepted") is not True
        or artifacts[0].get("created") is not True
        or artifacts[0].get("version") != 1
        or artifacts[0].get("name") != name
        or artifacts[0].get("sha256") != digest
    ):
        raise AssertionError(
            "Checkpoint lacks a single successful fresh artifact write and matching hash"
        )


def audit_record(root: Path, *, complete: bool = False) -> dict[str, Any]:
    """Audit saved evidence without trusting the model's or original observer's verdict."""
    identity = json.loads((root / "identity.json").read_text())
    status_path = root / "status.json"
    status = json.loads(status_path.read_text())
    now = time.time()
    if status.get("status") == "failed":
        raise AssertionError("Original observer reports a failed run")
    if not complete and now - status_path.stat().st_mtime > 90:
        raise AssertionError("Observer status is stale")
    if status.get("session_status") != "idle":
        if complete:
            raise AssertionError("A complete qualification cannot have a turn in progress")
        return {"qualified": False, "state": "turn_in_progress", "at": now}
    evidence = json.loads((root / "file-evidence.json").read_text())
    hashes = evidence["sha256"]
    workspace = root / "runtime/workspace"
    if not hashes or len(hashes) > COUNT:
        raise AssertionError("No verified checkpoints, or too many")
    if sorted(p.name for p in workspace.glob("batch-*.json")) != sorted(hashes):
        raise AssertionError("File evidence omits or invents a checkpoint")
    for number, (name, digest) in enumerate(sorted(hashes.items())):
        path = workspace / name
        raw = path.read_bytes()
        if name != f"batch-{number:03d}.json" or hashlib.sha256(raw).hexdigest() != digest:
            raise AssertionError("Checkpoint missing, reordered or changed")
        if json.loads(raw) != {
            "batch": number,
            "next": number + 1,
            "previous": number - 1,
            "marker": MARKER,
        }:
            raise AssertionError("Checkpoint forgot the original instruction")
        if path.stat().st_mtime > evidence["observed_at"][name] + 1:
            raise AssertionError("Checkpoint was written again after initial verification")
    transcript = json.loads((root / "transcript.json").read_text())["messages"]
    facts = verify_transcript(transcript, hashes, workspace)
    if len(hashes) < COUNT and (
        not status.get("goal", {}).get("active")
        or not status.get("loop", {}).get("active")
        or len(status.get("schedules", [])) != 1
        or status["schedules"][0]["id"] != status["loop"].get("pending_schedule_id")
    ):
        raise AssertionError("Incomplete run has lost its goal, loop or single durable wakeup")
    if complete:
        if (
            len(hashes) != COUNT
            or now - identity["started"] < 48 * INTERVAL
            or status.get("elapsed_monotonic_seconds", -1) < 48 * INTERVAL
        ):
            raise AssertionError(
                "A complete qualification requires all 49 checkpoints and 48 real hours"
            )
        manifest = json.loads((workspace / "final-manifest.json").read_text())
        if manifest != {"marker": MARKER, "files": sorted(hashes)}:
            raise AssertionError("Final manifest is incomplete or incorrect")
        if (
            status.get("goal", {}).get("clear_reason") != "goal_met"
            or status.get("goal", {}).get("active")
            or status.get("goal", {}).get("met") is not True
            or status.get("goal", {}).get("cleared") is not True
            or status.get("loop", {}).get("stop_reason") != "loop_goal_met"
            or status.get("loop", {}).get("active")
            or status.get("loop", {}).get("stopped") is not True
            or status.get("schedules")
        ):
            raise AssertionError("Completion left incorrect control state or pending work")
    return {
        "qualified": complete,
        "state": "complete" if complete else "partial",
        "at": now,
        **facts,
    }


def main() -> int:
    """Write a separate audit receipt; partial progress can never claim a full pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--complete", action="store_true")
    args = parser.parse_args()
    try:
        audit = audit_record(args.root.resolve(), complete=args.complete)
    except (AssertionError, KeyError, ValueError, OSError) as exc:
        audit = {"qualified": False, "state": "failed", "at": time.time(), "error": str(exc)}
    temporary = args.root / "independent-audit.tmp"
    temporary.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    temporary.replace(args.root / "independent-audit.json")
    print(json.dumps(audit))
    return int(audit["state"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
