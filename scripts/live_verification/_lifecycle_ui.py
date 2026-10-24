"""Hold actual model-created work for browser lifecycle interaction qualification."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import _common as common
import _lifecycle_evidence as evidence


def ui_cycle(call: Any, proof: Path, wid: str, agent: str, server_pid: int) -> dict[str, Any]:
    """Publish real owned task identities, then require browser actions and process settlement."""
    output = proof / "ui-gate"
    output.mkdir()
    workspace = proof / "workspace"
    paths = evidence.worker_files(workspace, "ui-control", "must-not-complete")
    sid = common.create_session(call, wid, "Actual lifecycle UI qualification")
    common.use_agent(call, sid, agent)
    command = common.quoted_command(sys.executable, str(paths["script"]))
    assignment = (
        f"Call shell_bash(command={command!r},background=true,timeout_s=0,cwd={str(workspace)!r}) "
        "exactly once then wait_tasks without a timeout. Preserve this accepted operation on resume. "
        "If cancelled and restarted with retained context, report its cancellation and finish; "
        "never replay the operation. Do not read files or submit other work."
    )
    evidence.model_turn(
        call,
        proof,
        sid,
        f"Call spawn_agent_task(agent='worker',task={assignment!r}) exactly once, then finish. "
        "Leave the task running. Report completion wakes without new work.",
        "spawn_agent_task",
    )
    child = next(row for row in evidence.tasks(call, sid) if row["task_kind"] == "Subagent")
    shell = evidence.require(
        lambda: next(
            (
                row
                for row in evidence.tasks(call, child["child_session_id"])
                if row["task_kind"] == "Shell"
            ),
            None,
        ),
        "actual UI descendant Shell",
    )
    identity = evidence.require(
        lambda: evidence.worker_identity(server_pid, paths["script"]),
        "actual UI process identity",
    )
    common.dump_json(
        output / "ready.json",
        {
            "parent": sid,
            "child": child,
            "shell": shell,
            "workspace": wid,
            "worker_identity": identity,
        },
    )
    print(f"UI ready: {output / 'ready.json'}", flush=True)
    evidence.require(
        lambda: (output / "browser-result.json").is_file(),
        "actual desktop/phone browser lifecycle controls",
        seconds=1200,
    )
    browser = json.loads((output / "browser-result.json").read_text(encoding="utf8"))
    assert browser["pass"] is True
    cancelled = evidence.task(call, sid, child["handle"])
    assert cancelled["effective_status"] == "cancelled"
    assert (
        evidence.task(call, child["child_session_id"], shell["handle"])["effective_status"]
        == "cancelled"
    )
    assert evidence.identity_absent(identity) and not paths["done"].exists()
    evidence.assert_no_replay(paths["starts"])
    for recipient in (sid, child["child_session_id"]):
        evidence.save_session(call, output, recipient)
    row = {"pass": True, "browser": browser, "cancelled": cancelled, "worker_absent": True}
    common.dump_json(output / "verdict.json", row)
    print("LIVE actual-service UI controls passed", flush=True)
    return row
