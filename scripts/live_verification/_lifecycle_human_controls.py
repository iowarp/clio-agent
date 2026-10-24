"""Actual model receipt of human task controls through the production HTTP doors."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import Any

import _common as common
import _lifecycle_evidence as evidence


def _notices(call: Any, sid: str, action: str, handle: str) -> list[dict[str, Any]]:
    return [
        row
        for row in evidence.mail(call, sid)
        if (notice := row.get("human_intervention", {})).get("action") == action
        and notice.get("handle") == handle
    ]


def _delivered(call: Any, sid: str, action: str, handle: str) -> list[dict[str, Any]] | None:
    rows = _notices(call, sid, action, handle)
    return rows if rows and all(row["delivered_at"] for row in rows) else None


def _reported(call: Any, sid: str, token: str) -> bool:
    return any(
        token in part.get("text", "")
        for row in common.session_messages(call, sid)
        if row.get("role") == "assistant"
        for part in row.get("parts", [])
        if part.get("type") == "text"
    )


def human_control_cycle(
    call: Any, proof: Path, wid: str, agent: str, number: int, server_pid: int
) -> dict[str, Any]:
    """Prove busy iteration injection, idle wake, owned cancellation and restart identity."""
    output = proof / f"cycle-{number}"
    output.mkdir()
    workspace = proof / "workspace"
    sentinel = workspace / "sentinel.txt"
    child_files = evidence.worker_files(workspace, f"human-{number}-child", secrets.token_hex(16))
    root_files = evidence.worker_files(workspace, f"human-{number}-root", secrets.token_hex(16))
    boundary_files = evidence.worker_files(
        workspace, f"human-{number}-boundary", secrets.token_hex(16)
    )
    child_command = common.quoted_command(sys.executable, str(child_files["script"]))
    root_command = common.quoted_command(sys.executable, str(root_files["script"]))
    boundary_command = common.quoted_command(sys.executable, str(boundary_files["script"]))
    nonce = "HUMAN_STEER_" + secrets.token_hex(12)
    sid = common.create_session(call, wid, f"human control cycle {number}")
    common.use_agent(call, sid, agent)
    assignment = (
        f"Start shell_bash(command={child_command!r}, background=true, timeout_s=0, "
        f"cwd={str(workspace)!r}) exactly once, then wait_tasks on its handle without a timeout. "
        "On pause/resume, retain that handle without replay. Receive human messages without "
        "replying to the parent. On a linked successor run, do not launch any operation: make "
        "one fresh get_task_result call on the retained prior Shell handle, report its actual "
        "outcome and finish. Cancelled work must not be restarted automatically."
    )
    submission = common.post_message(
        call,
        sid,
        f"Spawn worker with exactly this assignment: {assignment!r}. Then start your own "
        f"shell_bash(command={root_command!r}, background=true, timeout_s=0, "
        f"cwd={str(workspace)!r}) exactly once. Read fs_read_file(filepath={str(sentinel)!r}). "
        f"Then shell_bash(command={boundary_command!r}, background=false, timeout_s=60, "
        f"cwd={str(workspace)!r}) exactly once. This foreground command is an operator-controlled "
        "boundary gate; it will be released after human intervention. After it returns, "
        "query_tasks once and report every queued "
        "human_intervention's intervention_id, action, handle, phase, outcome and exact text. Then finish; "
        "never wait indefinitely or perform lifecycle controls yourself. On any later "
        "automatic wake, query_tasks once, report newly supplied human intervention actions, "
        "intervention_ids, handles, phases/outcomes and exact text, then finish. Do not replay, resume, restart "
        "or cancel work yourself, and do not reply to children.",
    )
    child = evidence.require(
        lambda: next(
            (row for row in evidence.tasks(call, sid) if row["task_kind"] == "Subagent"), None
        ),
        "parent model submitted real child",
    )
    handle, child_sid = child["handle"], child["child_session_id"]
    root_shell = evidence.require(
        lambda: next(
            (
                row
                for row in evidence.tasks(call, sid)
                if row["task_kind"] == "Shell" and row["owner"]["session_id"] == sid
            ),
            None,
        ),
        "parent model submitted independent background Shell",
    )
    child_shell = evidence.require(
        lambda: next(
            (row for row in evidence.tasks(call, child_sid) if row["task_kind"] == "Shell"),
            None,
        ),
        "child model submitted background Shell",
    )
    evidence.require(
        lambda: boundary_files["starts"].exists(),
        "parent entered its real foreground tool before human intervention",
    )
    child_process = evidence.require(
        lambda: evidence.worker_identity(server_pid, child_files["script"]), "actual child process"
    )
    root_process = evidence.require(
        lambda: evidence.worker_identity(server_pid, root_files["script"]), "actual parent process"
    )
    boundary_process = evidence.require(
        lambda: evidence.worker_identity(server_pid, boundary_files["script"]),
        "actual boundary process",
    )
    assert call("GET", f"/v1/sessions/{sid}")["status"] == "running"
    stop = call("POST", f"/v1/sessions/{child_sid}/cancel", {}, ok=(204,))
    evidence.require(
        lambda: evidence.settled_turn(call, child_sid), "human Stop ended child model turn"
    )
    assert not evidence.identity_absent(child_process), "Stop cancelled accepted background work"
    pause = call("POST", f"/v1/sessions/{sid}/agent-controls/{handle}/pause", {})
    evidence.require(
        lambda: evidence.task(call, sid, handle)["effective_status"] == "paused",
        "human pause settled the child model only",
    )
    assert not evidence.identity_absent(child_process)
    steer = call(
        "POST",
        f"/v1/sessions/{sid}/agent-messages",
        {"target": handle, "message": nonce},
        ok=(202,),
    )
    assert steer["disposition"] == "held"
    resume = call("POST", f"/v1/sessions/{sid}/agent-controls/{handle}/resume", {})
    boundary_files["release"].write_text(
        "human controls accepted; allow next model iteration", encoding="utf8"
    )
    evidence.require(
        lambda: _delivered(call, sid, "steer", handle),
        "parent collected human steering through its queue",
    )
    evidence.require(
        lambda: _reported(call, sid, nonce),
        "actual parent model reported nonce known only to human child-steering notice",
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "parent yielded after notice")
    assert not child_files["done"].exists() and not root_files["done"].exists()
    assert not evidence.identity_absent(child_process)
    boundary_event = evidence.tool_events(proof, sid, "shell_bash")[-1]
    queries = evidence.tool_events(proof, sid, "query_tasks")
    assert queries and queries[-1]["occurred_at"] >= boundary_event["occurred_at"]
    assert evidence.identity_absent(boundary_process)
    busy_notices = evidence.mail(call, sid)
    assert {row["human_intervention"]["action"] for row in busy_notices} >= {
        "pause",
        "steer",
        "resume",
        "stop",
    }
    for row in busy_notices:
        assert _reported(call, sid, row["human_intervention"]["intervention_id"])
    # Isolate idle mail wake from completion wake: pause creates no terminal result.
    idle_pause = call("POST", f"/v1/sessions/{sid}/agent-controls/{handle}/pause", {})
    idle_pause_id = _notices(call, sid, "pause", handle)[-1]["human_intervention"][
        "intervention_id"
    ]
    evidence.require(
        lambda: _reported(call, sid, idle_pause_id), "idle parent model received human pause alone"
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "idle pause-notice turn ended")
    assert not child_files["done"].exists() and not root_files["done"].exists()
    evidence.require(
        lambda: evidence.task(call, sid, handle)["effective_status"] == "paused",
        "child held during parent wake",
    )
    idle_resume = call("POST", f"/v1/sessions/{sid}/agent-controls/{handle}/resume", {})
    idle_resume_id = _notices(call, sid, "resume", handle)[-1]["human_intervention"][
        "intervention_id"
    ]
    evidence.require(lambda: _reported(call, sid, idle_resume_id), "model received human resume")
    evidence.require(lambda: evidence.settled_turn(call, sid), "resume-notice wake settled")
    # The parent is idle. Human cancellation is another input, without a user POST to it.
    idle_count = len(common.session_messages(call, sid))
    root_cancel = call(
        "POST", f"/v1/sessions/{sid}/async-tasks/cancel", {"tasks": root_shell["handle"]}
    )
    assert not root_cancel["errors"]
    evidence.require(
        lambda: _delivered(call, sid, "cancel", root_shell["handle"]),
        "idle parent woke for human cancellation notice",
    )
    root_cancel_id = _notices(call, sid, "cancel", root_shell["handle"])[-1]["human_intervention"][
        "intervention_id"
    ]
    evidence.require(
        lambda: _reported(call, sid, root_cancel_id), "model received Shell cancellation notice"
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "cancellation-notice wake settled")
    assert len(common.session_messages(call, sid)) > idle_count
    evidence.require(
        lambda: evidence.identity_absent(root_process), "parent Shell physically ended"
    )
    child_cancel = call("POST", f"/v1/sessions/{sid}/async-tasks/cancel", {"tasks": handle})
    assert not child_cancel["errors"]
    evidence.require(
        lambda: evidence.task(call, sid, handle)["effective_status"] == "cancelled",
        "human Subagent cancellation settled subtree",
    )
    evidence.require(
        lambda: evidence.identity_absent(child_process), "child Shell physically ended"
    )
    evidence.require(
        lambda: _delivered(call, sid, "cancel", handle), "parent received subtree notice"
    )
    child_cancel_id = _notices(call, sid, "cancel", handle)[-1]["human_intervention"][
        "intervention_id"
    ]
    evidence.require(
        lambda: _reported(call, sid, child_cancel_id), "model received subtree cancellation notice"
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "subtree notice turn ended")
    restart = call("POST", f"/v1/sessions/{sid}/agent-controls/{handle}/restart", {})
    assert restart["handle"] != handle and restart["supersedes_handle"] == handle
    evidence.require(
        lambda: _delivered(call, sid, "restart", handle), "parent received new run handle"
    )
    restart_id = _notices(call, sid, "restart", handle)[-1]["human_intervention"]["intervention_id"]
    evidence.require(lambda: _reported(call, sid, restart_id), "model received restart notice")
    evidence.require(
        lambda: evidence.task(call, sid, restart["handle"])["effective_status"] in evidence.SETTLED,
        "successor settled without replaying owned command",
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "restart-notice wake settled")
    evidence.assert_no_replay(child_files["starts"])
    evidence.assert_no_replay(root_files["starts"])
    mail_before = evidence.mail(call, sid)
    identities_before = [(row["message_id"], row["delivered_at"]) for row in mail_before]
    evidence.model_turn(
        call,
        proof,
        sid,
        f"Read fs_read_file(filepath={str(sentinel)!r}), then finish. No task controls or messages.",
        "fs_read_file",
    )
    assert [
        (row["message_id"], row["delivered_at"]) for row in evidence.mail(call, sid)
    ] == identities_before
    evidence.save_session(call, output, sid)
    evidence.save_session(call, output, child_sid)
    row = {
        "pass": True,
        "parent": sid,
        "child": child_sid,
        "handle": handle,
        "root_shell": root_shell["handle"],
        "child_shell": child_shell["handle"],
        "operator_message_id": submission["message_id"],
        "steer_nonce": nonce,
        "pause": pause,
        "stop": stop,
        "idle_pause": idle_pause,
        "idle_resume": idle_resume,
        "steer": steer,
        "resume": resume,
        "root_cancel": root_cancel,
        "child_cancel": child_cancel,
        "restart": restart,
        "busy_notices": busy_notices,
        "final_mail": mail_before,
        "parent_boundary_invocation": boundary_event["payload"]["call_id"],
        "parent_queries": [event["payload"]["call_id"] for event in queries],
        "root_process": root_process,
        "child_process": child_process,
        "boundary_process": boundary_process,
        "processes_absent": True,
        "accepted_operations_replayed": False,
    }
    common.dump_json(output / "verdict.json", row)
    print(f"LIVE human intervention cycle {number} passed", flush=True)
    return row
