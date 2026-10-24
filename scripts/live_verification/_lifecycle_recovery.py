"""Real service interruption and retained-context restart without operation replay."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import _common as common
import _lifecycle_evidence as evidence


def recovery_cycle(
    call: Any,
    proof: Path,
    wid: str,
    agent: str,
    server_pid: int,
    restart_service: Callable[[], int],
) -> dict[str, Any]:
    """Interrupt an owned API, recover persisted failure, then ask a real model to restart."""
    output = proof / "service-recovery"
    output.mkdir()
    workspace = proof / "workspace"
    paths = evidence.worker_files(workspace, "recovery", "must-not-replay")
    sid = common.create_session(call, wid, "actual interrupted-run recovery")
    common.use_agent(call, sid, agent)
    command = common.quoted_command(sys.executable, str(paths["script"]))
    assignment = (
        f"Call shell_bash(command={command!r},background=true,timeout_s=0,cwd={str(workspace)!r}) "
        "exactly once and immediately call wait_tasks on the returned handle without a timeout. "
        "Do not end your model turn before making that wait call: this mission verifies "
        "an actual in-flight waiter during service interruption. Do not read files or "
        "start other work. "
        "If service interruption ends this run and a retained-context successor is started, "
        "report the previous failed/interrupted outcome and finish. Never replay the operation."
    )
    evidence.model_turn(
        call,
        proof,
        sid,
        f"Call spawn_agent_task(agent='worker',task={assignment!r}) exactly once, then finish. "
        "Leave the task running. On completion wake report the outcome without new work.",
        "spawn_agent_task",
    )
    child = next(row for row in evidence.tasks(call, sid) if row["task_kind"] == "Subagent")
    child_sid, handle = child["child_session_id"], child["handle"]
    shell = evidence.require(
        lambda: next(
            (row for row in evidence.tasks(call, child_sid) if row["task_kind"] == "Shell"), None
        ),
        "real Shell accepted before isolated service interruption",
    )
    evidence.require(
        lambda: evidence.tool_events(proof, child_sid, "wait_tasks", complete=False),
        "real child committed to waiting before service interruption",
    )
    identity = evidence.require(
        lambda: evidence.worker_identity(server_pid, paths["script"]),
        "exact owned process identity before service interruption",
    )
    for recipient in (sid, child_sid):
        evidence.save_session(call, output, recipient)
    new_server_pid = restart_service()
    failed = evidence.require(
        lambda: (
            row
            if (row := evidence.task(call, sid, handle))["effective_status"] == "failed"
            else None
        ),
        "interrupted Subagent recovered as failed with its original handle",
    )
    assert failed.get("raw_status") == "failed" and evidence.identity_absent(identity)
    old_shell = evidence.task(call, child_sid, shell["handle"])
    assert old_shell["effective_status"] in {"interrupted", "failed"}
    evidence.assert_no_replay(paths["starts"])
    restart = evidence.model_turn(
        call,
        proof,
        sid,
        f"Call restart_agent(target={handle!r}) exactly once with retained context. "
        "Finish. Never resubmit an accepted operation or start another Shell command.",
        "restart_agent",
    )
    ack = evidence.observation(restart["invocation"])
    assert ack["handle"] != handle and ack["supersedes_handle"] == handle
    successor = evidence.require(
        lambda: (
            row
            if (row := evidence.task(call, sid, ack["handle"]))["effective_status"] == "completed"
            else None
        ),
        "real model completed retained-context restart after service interruption",
    )
    assert evidence.task(call, sid, handle)["raw_status"] == "failed"
    assert not evidence.tool_events(proof, successor["child_session_id"], "shell_bash")
    evidence.assert_no_replay(paths["starts"])
    for recipient in (sid, child_sid, successor["child_session_id"]):
        evidence.save_session(call, output, recipient)
    row = {
        "pass": True,
        "parent": sid,
        "original": handle,
        "failed": failed,
        "original_shell": old_shell,
        "worker_identity": identity,
        "worker_absent": True,
        "restart": restart,
        "successor": successor,
        "restarted_api_pid": new_server_pid,
    }
    common.dump_json(output / "verdict.json", row)
    print("LIVE interrupted-run recovery/restart passed", flush=True)
    return row
