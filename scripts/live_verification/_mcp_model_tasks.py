"""Actual-model MCP task acceptance against the real FastMCP conformance server.

The workload is explicitly synthetic; protocol, transport, CLIO dispatch,
supervision, human input and model execution are production implementations.
The owning session_workflow_tasks controller removes all transient sessions.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

import _common as common
import _lifecycle_evidence as evidence
from _session_workflow_receipts import model_receipt


def testing_agent(call: Any, workspace: Path, wid: str) -> str:
    """Install the task testing agent with the real conformance MCP backend; return its ref."""
    core = Path(__file__).resolve().parents[2]
    # ``python`` resolves to the server's interpreter (``common.boot_server`` and the
    # owning controller put it first on PATH).
    command = ["python", str(core / "tests/test_tools/mcp_exerciser.py")]
    pack = common.materialize_testing_marketplace(
        core / "scripts/live_verification/agents/task-testing", workspace, {"v2ex": command}
    )
    prompt = common.testing_prompt(pack)
    prompt.write_text(
        prompt.read_text()
        + "\nOn an automatic completion wake, query_tasks and get_task_result for the "
        "supplied handle, report its exact handle, status and result, then finish. "
        "Report every supplied human intervention_id. Do not submit new work or reply. "
        "Use only declared arguments; correct input validation errors without replaying "
        "an accepted operation or retrying a permission denial.\n"
    )
    return common.install_testing_agent(call, pack)


def _start(call: Any, root: Path, sid: str, tool: str, submission: str) -> dict[str, Any]:
    """Require the model's accepted handle and independent action while it is active."""
    sentinel = root / "workspace/sentinel.txt"
    common.post_message(
        call,
        sid,
        submission + " Submit this operation exactly once. Immediately query_tasks(kind='MCP'), "
        f"then fs_read_file(filepath={str(sentinel)!r}), then query_tasks(kind='MCP') again. "
        "Then wait_tasks on its handle with timeout_s=0.1 and observe_tasks on that handle. "
        "Report actual observations and finish this turn without collecting a terminal "
        "result or waiting again. The operator will supply human input if requested. "
        "Accepted tasks continue and their results will wake you automatically; on that "
        "wake query and read the supplied result, report its handle/status/result and "
        "any human intervention_id, then finish. Never replay accepted work.",
    )
    accepted = evidence.require(
        lambda: next(iter(evidence.tool_events(root, sid, tool)), None),
        f"model accepted {tool}",
    )
    observation = evidence.observation(accepted)
    assert observation["accepted"] and observation["kind"] == "MCP"
    handle = observation["handle"]
    for name in ("query_tasks", "fs_read_file", "wait_tasks", "observe_tasks"):
        evidence.require(
            lambda name=name: evidence.tool_events(root, sid, name), f"model invoked {name}"
        )
    evidence.require(lambda: evidence.settled_turn(call, sid), "MCP parent ended before result")
    queries = evidence.tool_events(root, sid, "query_tasks")
    assert len(queries) >= 2
    reads = evidence.tool_events(root, sid, "fs_read_file")
    all_events = evidence.events(root, sid)
    # Invocation IDs are stable even when a trace has no optional event_id.
    indexes = {
        event["payload"]["call_id"]: index
        for index, event in enumerate(all_events)
        if event.get("event_type") == "tool.call.completed"
    }
    assert indexes[accepted["payload"]["call_id"]] < indexes[reads[0]["payload"]["call_id"]]
    assert indexes[reads[0]["payload"]["call_id"]] < indexes[queries[-1]["payload"]["call_id"]]
    observed = evidence.observation(queries[-1])
    active = next(row for row in observed["tasks"] if row["handle"] == handle)
    assert active["effective_status"] not in evidence.SETTLED
    wait = evidence.observation(evidence.tool_events(root, sid, "wait_tasks")[-1])
    assert handle in wait["pending"] and wait["timed_out"]
    assert not evidence.tool_events(root, sid, "get_task_result")
    return {
        "handle": handle,
        "session": sid,
        "active_status": active["effective_status"],
        "progress": active.get("progress"),
        "invocations": [
            accepted["payload"]["call_id"],
            *[event["payload"]["call_id"] for event in [*queries, *reads]],
        ],
        "expired_wait_left_task_active": True,
        "initial_message_ids": [message["id"] for message in common.session_messages(call, sid)],
    }


def _finish(
    call: Any, root: Path, sid: str, handle: str, status: str, previous: list[str]
) -> dict[str, Any]:
    """Require owner settlement, automatic model collection and no duplicate notice."""
    evidence.require(
        lambda: evidence.task(call, sid, handle)["effective_status"] == status,
        f"MCP owner settled {status}",
    )
    evidence.require(
        lambda: any(
            message["id"] not in previous
            and message.get("role") == "assistant"
            and handle in (text := json.dumps(message.get("parts", [])))
            and status in text
            for message in common.session_messages(call, sid)
        ),
        "new automatic model response acknowledged terminal handle",
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "MCP completion wake settled")
    receipt = model_receipt(call, sid, handle, status)
    assert receipt["message"] not in previous
    result_reads = evidence.tool_events(root, sid, "get_task_result")
    assert result_reads, "Actual model must read the terminal result"
    raw = evidence.retained_task(call, sid, handle)
    assert raw["result"] is not None
    notices = {
        row["message_id"]: row.get("delivered_at")
        for row in evidence.mail(call, sid)
        if row.get("correlation_id") == handle
    }
    assert notices and all(notices.values())
    previous_reads = len(evidence.tool_events(root, sid, "fs_read_file"))
    common.post_message(call, sid, "Read the sentinel file once, then finish. Submit no tasks.")
    evidence.require(
        lambda: len(evidence.tool_events(root, sid, "fs_read_file")) > previous_reads,
        "later model turn read sentinel",
    )
    evidence.require(lambda: evidence.settled_turn(call, sid), "later model turn settled")
    later = {
        row["message_id"]: row.get("delivered_at")
        for row in evidence.mail(call, sid)
        if row.get("correlation_id") == handle
    }
    assert later == notices, "A subsequent turn must not redeliver or consume twice"
    return {
        "model_receipt": receipt,
        "record": {
            key: raw.get(key)
            for key in (
                "key",
                "handle",
                "status",
                "effective_status",
                "cancel_requested",
                "cancel_acknowledged",
                "connection_freshness",
                "invocation_id",
            )
        },
        "result_sha256": hashlib.sha256(
            json.dumps(raw["result"], sort_keys=True).encode()
        ).hexdigest(),
        "notices": notices,
        "later_turn_no_duplicate": True,
    }


def cycle(call: Any, root: Path, wid: str, pack: str, number: int) -> dict[str, Any]:
    """Prove async stdio work, human input, and human cancellation of input-held work."""
    rows = []
    for mode in ("slow", "answer", "cancel"):
        sid = common.create_session(call, wid, f"MCP {number} {mode}")
        common.use_agent(call, sid, pack)
        tool = "v2ex_staller" if mode == "slow" else "v2ex_guarded_input"
        submission = (
            "Call v2ex_staller(seconds=120, steps=24)."
            if mode == "slow"
            else "Call v2ex_guarded_input()."
        )
        row = _start(call, root, sid, tool, submission)
        handle = row["handle"]
        if mode != "slow":
            question = evidence.require(
                lambda sid=sid: next(iter(common.pending_questions(call, sid)), None),
                "real MCP input surfaced on human question route",
            )
            row["question_id"] = question["id"]
            if mode == "answer":
                nonce = "INPUT_" + secrets.token_hex(12)
                common.answer_question(call, sid, question["id"], json.dumps({"value": nonce}))
                row["answer_nonce"] = nonce
            else:
                acknowledgement = call(
                    "POST", f"/v1/sessions/{sid}/async-tasks/cancel", {"tasks": handle}
                )
                assert not acknowledgement["errors"]
        row.update(
            _finish(
                call,
                root,
                sid,
                handle,
                "cancelled" if mode == "cancel" else "completed",
                row["initial_message_ids"],
            )
        )
        raw = evidence.retained_task(call, sid, handle)
        if mode == "answer":
            assert row["answer_nonce"] in json.dumps(raw["result"])
            assert len(raw["input_answers"]) == 1
            assert any(
                row["answer_nonce"] in part.get("text", "")
                for message in common.session_messages(call, sid)
                if message.get("role") == "assistant"
                for part in message.get("parts", [])
            ), "The actual model must report the nonce supplied only through human input"
        elif mode == "cancel":
            assert raw["cancel_requested"] and raw["cancel_acknowledged"]
            questions = call("GET", f"/v1/sessions/{sid}/questions")["questions"]
            assert all(question["status"] != "pending" for question in questions)
            row["human_notices"] = [
                item["human_intervention"]
                for item in evidence.mail(call, sid)
                if item.get("human_intervention", {}).get("handle") == handle
            ]
            assert row["human_notices"]
            for item in row["human_notices"]:
                assert any(
                    item["intervention_id"] in part.get("text", "")
                    for message in common.session_messages(call, sid)
                    if message.get("role") == "assistant"
                    for part in message.get("parts", [])
                )
        else:
            assert "stalled-through" in json.dumps(raw["result"])
        assert len(evidence.tool_events(root, sid, tool)) == 1, "No operation replay"
        rows.append({"mode": mode, **row})
    return {"pass": True, "cycle": number, "routes": rows}
