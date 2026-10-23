"""Collect model-authored diagrams and exact capture evidence from real CLIO traces.

Use the raw react.step.completed observations, not a posthoc name/argument merge:
repeated capture arguments can have different results as the viewer changes.
This collector validates custody; semantic and visual grading remain explicit reviews.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def collect_run(record_path: Path, traces: Path) -> dict[str, Any]:
    """Read one submission and retain immutable model observations with checked images."""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    session_id = record["session_id"]
    trace_path = traces / f"{session_id}.semantic.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    steps = [row for row in events if row["event_type"] == "react.step.completed"]
    calls = [call for step in steps for call in step["payload"].get("tool_calls", [])]
    captures: list[dict[str, Any]] = []
    for call in calls:
        if call["name"] != "capture_a2ui_surface":
            continue
        observation = call["observation"]
        if not isinstance(observation, dict) or "capture" not in observation:
            captures.append({"call_id": call["id"], "error": observation})
            continue
        image_path = Path(observation["path"])
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest != observation["sha256"]:
            raise ValueError(f"Capture hash mismatch: {image_path}")
        captures.append(
            {
                "call_id": call["id"],
                "sha256": digest,
                "path": str(image_path),
                "capture": observation["capture"],
            }
        )
    publications = [
        call["observation"] for call in calls if call["name"] == "publish_dashboard_report"
    ]
    skill_loads = [call["args"] for call in calls if call["name"] == "load_skill"]
    skill_events = [row["payload"] for row in events if row["event_type"] == "skill.loaded"]
    turns: list[dict[str, Any]] = []
    for turn_id in dict.fromkeys(step.get("turn_id", "") for step in steps):
        turn_steps = [step for step in steps if step.get("turn_id", "") == turn_id]
        turn_calls = [call for step in turn_steps for call in step["payload"].get("tool_calls", [])]
        turns.append(
            {
                "turn_id": turn_id,
                "steps": len(turn_steps),
                "tool_counts": dict(Counter(call["name"] for call in turn_calls)),
                "capture_call_ids": [
                    call["id"] for call in turn_calls if call["name"] == "capture_a2ui_surface"
                ],
            }
        )
    result = {
        "session_id": session_id,
        "stage": record["stage"],
        "model": record["model"],
        "workspace": record["workspace"],
        "trace": str(trace_path),
        "steps": len(steps),
        "tool_counts": dict(Counter(call["name"] for call in calls)),
        "skill_loads": skill_loads,
        "loaded_content": skill_events,
        "captures": captures,
        "publications": publications,
        "calls": calls,
        "turns": turns,
    }
    return result


def main() -> None:
    """Write a receipt per submitted run, preserving failures as well as successes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Evidence directory containing submission JSON")
    parser.add_argument("--traces", type=Path, help="Private service semantic_traces directory")
    args = parser.parse_args()
    traces = args.traces or args.root / "home/state/traces/semantic_traces"
    summary: list[dict[str, Any]] = []
    for path in sorted(args.root.glob("*-gpt-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if "session_id" not in record or "submission" not in record:
            continue
        receipt = collect_run(path, traces)
        output = args.root / f"{path.stem}-receipt.json"
        output.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        brief = {key: receipt[key] for key in ("session_id", "stage", "model", "tool_counts")}
        brief["captures"] = len([row for row in receipt["captures"] if "sha256" in row])
        summary.append(brief)
    (args.root / "benchmark-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
