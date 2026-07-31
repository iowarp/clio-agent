"""Generate the committed clio-agent schema-equality expectations.

The expectation is derived from the live invoker dataclasses and their wire
projectors, never maintained by hand. Regenerate after an intentional convergence
contract revision::

    uv run --no-sync --no-cache python scripts/generate_schema_equality_expectations.py

CI and local checks can compare without writing::

    uv run --no-sync --no-cache python scripts/generate_schema_equality_expectations.py --check

``--seed-divergent`` exists only to reproduce the P2.3 failing-first acceptance
proof. It deterministically adds one nonexistent TaskSpec wire field; immediately
run the normal regeneration command after capturing the red test output.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from clio_agent.gact.agents.invoker import (
    RELAY_STATE_MAP,
    TaskEvent,
    TaskHandle,
    TaskResult,
    TaskSpec,
    spec_to_wire,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTATION_PATH = ROOT / "tests" / "schema_expectations" / "invoker_wire_shapes.json"
GENERATE_COMMAND = (
    "uv run --no-sync --no-cache python scripts/generate_schema_equality_expectations.py"
)


def _required_fields(cls: type[Any]) -> list[str]:
    return [
        item.name
        for item in fields(cls)
        if item.default is MISSING and item.default_factory is MISSING
    ]


def _shape(cls: type[Any], wire: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "dataclass_fields": [item.name for item in fields(cls)],
        "required_fields": _required_fields(cls),
        "wire_fields": list(wire),
    }


def build_expectations() -> dict[str, Any]:
    """Return the deterministic invoker and relay convergence expectation."""

    spec = TaskSpec(
        child_expert_id="child",
        task_text="task",
        parent_session_id="parent-session",
        requesting_expert_id="requester",
        parent_turn_id="parent-turn",
        depth=2,
        mode="sync",
        workflow_state={"state": "bound"},
        fanout_bound=3,
        seed_context="context",
        skip_declared_check=True,
        workspace_id="workspace",
        session_mode="plan",
        session_scope_metadata={"scope": "bound"},
    )
    handle = TaskHandle(
        task_id="task",
        parent_session_id="parent-session",
        child_session_id="child-session",
    )
    result = TaskResult(
        task_id="task",
        parent_session_id="parent-session",
        child_session_id="child-session",
    )
    event = TaskEvent(
        event_type="agent.task.started",
        task_id="task",
        session_id="parent-session",
        status="running",
    )
    return {
        "_generated_by": GENERATE_COMMAND,
        "_note": "Generated file; change the contract, then regenerate. Never edit by hand.",
        "invoker_types": {
            "TaskEvent": _shape(TaskEvent, event.to_wire()),
            "TaskHandle": _shape(TaskHandle, handle.to_wire()),
            "TaskResult": _shape(TaskResult, result.to_wire()),
            "TaskSpec": _shape(TaskSpec, spec_to_wire(spec)),
        },
        "relay_state_map": RELAY_STATE_MAP,
    }


def render_expectations(*, seed_divergent: bool = False) -> str:
    """Render expectations as stable, newline-terminated JSON."""

    expectation = build_expectations()
    if seed_divergent:
        expectation["invoker_types"]["TaskSpec"]["wire_fields"].append("known_divergent_seed")
    return json.dumps(expectation, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    """Write or check the committed expectation file."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if regeneration would differ")
    mode.add_argument(
        "--seed-divergent",
        action="store_true",
        help="write the deterministic failing-first fixture",
    )
    args = parser.parse_args()
    rendered = render_expectations(seed_divergent=args.seed_divergent)
    if args.check:
        committed = (
            EXPECTATION_PATH.read_text(encoding="utf-8") if EXPECTATION_PATH.is_file() else ""
        )
        if committed != rendered:
            raise SystemExit(f"stale schema expectation; regenerate with: {GENERATE_COMMAND}")
        print(f"OK: {EXPECTATION_PATH.relative_to(ROOT)} is deterministic")
        return 0

    EXPECTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPECTATION_PATH.write_text(rendered, encoding="utf-8")
    qualifier = "known-divergent " if args.seed_divergent else ""
    print(f"wrote {qualifier}{EXPECTATION_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
