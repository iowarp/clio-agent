"""Operation outcomes must follow owned task evidence, not submission success."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.task_types import BackendReference, TaskOwner, TaskSnapshot, TaskStatus
from clio_agent.gact.native_work import now
from clio_agent.gact.types import Message, Part
from tests.test_gact.task_records import record_assignment_fixture

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
CALL = "call_owned_execution"


def _transcript(app: Any, sid: str) -> None:
    """Keep the actual acceptance response in a sealed transcript."""
    stamp = now()
    receipt = {
        "accepted": True,
        "kind": "MCP",
        "handle": "task_" + uuid4().hex,
        "status": "running",
    }
    app.state.messages[sid] = [
        Message(
            id="msg_accepted",
            session_id=sid,
            role="assistant",
            created_at=stamp,
            updated_at=stamp,
            stop_reason="end_turn",
            parts=[
                Part(
                    id="part_call",
                    type="tool_call",
                    call_id=CALL,
                    tool_name="remote_fetch",
                    input={"url": "https://example.org/source"},
                    metadata={"started_at": stamp},
                ),
                Part(
                    id="part_receipt",
                    type="tool_result",
                    call_id=CALL,
                    tool_name="remote_fetch",
                    structured_content=receipt,
                    is_error=False,
                    metadata={"completed_at": stamp},
                ),
            ],
        )
    ]
    app.state.sessions.update(sid, message_count=1)


def _task(
    app: Any,
    sid: str,
    status: TaskStatus,
    *,
    failed: bool = False,
    body: str = "Source document",
    malformed: bool = False,
    include_result: bool = True,
    structured: dict[str, Any] | None = None,
    tool_name: str = "remote_fetch",
) -> TaskSnapshot:
    """Record genuine Core custody/result references without running a model."""
    history = app.state.task_history
    assignment = history.latest_assignment(sid)
    assert assignment is not None
    stamp = now()
    terminal = status in {"completed", "failed", "cancelled"}
    result = None
    if terminal and include_result:
        raw = (
            {"invalid": True}
            if malformed
            else {
                "task_id": "remote-task",
                "status": "cancelled" if status == "cancelled" else "completed",
                "created_at": stamp,
                "last_updated_at": stamp,
                "ttl_ms": 60000,
                "result_type": "complete",
                "result": None
                if status == "cancelled"
                else {
                    "content": [{"type": "text", "text": body}],
                    "isError": failed,
                    **({"structuredContent": structured} if structured is not None else {}),
                },
            }
        )
        result = history.write_content(sid, raw)
    receipts = [
        part.structured_content
        for message in app.state.messages.get(sid, ())
        for part in message.parts
        if part.call_id == CALL and part.type == "tool_result" and part.structured_content
    ]
    task = TaskSnapshot(
        handle=receipts[0]["handle"] if receipts else "task_" + uuid4().hex,
        kind="MCP",
        owner=TaskOwner(
            human_id=app.state.human_principal,
            session_id=sid,
            assignment_id=assignment.handle,
            parent_handle=assignment.handle,
        ),
        label="Read source",
        description="Read a source document",
        description_missing=False,
        invocation_id=CALL,
        tool=tool_name,
        created_at=stamp,
        updated_at=stamp,
        backend=BackendReference(owner="external_mcp", server="web", task_id="remote-task"),
        status=status,
        raw_status="completed" if terminal else status,
        settlement="external_acknowledged" if terminal else "unknown",
        terminal_at=stamp if terminal else None,
        result=result,
    )
    history.record_snapshot(task, observation_id=uuid4().hex)
    return task


def _read(client: TestClient, sid: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{sid}/messages", headers=HEADERS)
    assert response.status_code == 200, response.text
    return next(tool for tool in response.json()["tools"] if tool["id"] == CALL)


def test_failed_terminal_result_updates_original_invocation_without_rewriting_receipt(
    world: tuple[Any, TestClient, str],
) -> None:
    """The actual failure is readable on reload, while the model observation stays exact."""
    app, client, sid = world
    _transcript(app, sid)
    original = app.state.messages[sid][0].to_wire()
    task = _task(app, sid, "failed", failed=True, body="Resource exceeded the fetch size limit.")
    tool = _read(client, sid)
    assert tool["state"] == "failed"
    assert tool["execution_task"]["handle"] == task.handle
    assert tool["submission"]["state"] == "succeeded"
    assert tool["submission"]["output"]["accepted"] is True
    assert any("fetch size limit" in block["text"] for block in tool["presentation"]["blocks"])
    assert app.state.messages[sid][0].to_wire() == original


@pytest.mark.parametrize("status", ["accepted", "queued", "running", "input_required", "paused"])
def test_accepted_work_is_not_green_after_the_submitting_turn_ends(
    world: tuple[Any, TestClient, str],
    status: TaskStatus,
) -> None:
    """An ended parent turn does not settle its submitted operation."""
    app, client, sid = world
    _transcript(app, sid)
    _task(app, sid, status)
    tool = _read(client, sid)
    assert tool["state"] in {"pending", "running"}
    assert tool["execution_task"]["status"] == status
    assert "completed_at" not in tool


def test_successful_terminal_content_is_available_on_its_invocation(
    world: tuple[Any, TestClient, str],
) -> None:
    """Actual document content, rather than its acceptance receipt, owns the result."""
    app, client, sid = world
    _transcript(app, sid)
    _task(app, sid, "completed", body="Inspected source document.")
    tool = _read(client, sid)
    assert tool["state"] == "succeeded"
    assert tool["output"]["content"][0]["text"] == "Inspected source document."
    assert tool["presentation"]["blocks"][0]["text"] == "Inspected source document."


def test_malformed_terminal_body_is_visible_uncertainty(
    world: tuple[Any, TestClient, str],
) -> None:
    """A bad record must not hide the whole conversation or turn into success."""
    app, client, sid = world
    _transcript(app, sid)
    _task(app, sid, "completed", malformed=True)
    tool = _read(client, sid)
    assert tool["state"] == "unknown"
    assert "could not be decoded" in tool["error"]


def test_receipt_shaped_synchronous_json_does_not_create_an_execution_relation(
    world: tuple[Any, TestClient, str],
) -> None:
    """Backend prose/payload fields cannot forge application task custody."""
    app, client, sid = world
    _transcript(app, sid)
    tool = _read(client, sid)
    assert tool["state"] == "succeeded"
    assert "execution_task" not in tool


def test_unrelated_session_task_with_same_call_id_is_not_joined(
    world: tuple[Any, TestClient, str],
) -> None:
    """The invocation ID is only meaningful within its original owner session."""
    app, client, sid = world
    _transcript(app, sid)
    other = client.post("/v1/sessions", json={"title": "Other owner"}).json()["id"]
    record_assignment_fixture(app, other)
    _task(app, other, "failed", failed=True)
    assert "execution_task" not in _read(client, sid)


@pytest.mark.parametrize("status", ["running", "failed"])
def test_live_completion_uses_task_outcome_and_keeps_rpc_receipt(
    world: tuple[Any, TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
    status: TaskStatus,
) -> None:
    """The SSE projection and reload agree even when task settlement beats the receipt."""
    from clio_agent.gact.protocol.v3.event import event_to_v3
    from clio_agent.gact.tool_completion_delivery import terminal_tool_delivery

    app, _client, sid = world
    _transcript(app, sid)
    _task(app, sid, status, failed=status == "failed", body="Fetch exceeded its limit.")
    events = []
    with monkeypatch.context() as capture:
        publish = app.state.bus.publish
        capture.setattr(
            app.state.bus, "publish", lambda event: (events.append(event), publish(event))
        )
        payload = {
            "call_id": CALL,
            "tool": "remote_fetch",
            "ok": True,
            "result": {"accepted": True},
        }
        with terminal_tool_delivery(app, sid, payload):
            pass
    completed = next(event for event in events if event.type == "tool.call.completed")
    entity = event_to_v3(completed)["payload"]
    assert entity["state"] == ("running" if status == "running" else "failed")
    assert entity["submission"]["state"] == "succeeded"
    assert entity["input"] == {"url": "https://example.org/source"}
    assert completed.payload["ok"] is True
    assert completed.payload["result"] == {"accepted": True}
    hint = next(event for event in events if event.type == "task.updated")
    assert hint.payload["owner_session_id"] == sid
    assert hint.payload["invocation_id"] == CALL
    assert "result" not in hint.payload


def test_task_presentation_pages_follow_exact_pinned_result(
    world: tuple[Any, TestClient, str],
) -> None:
    """A long terminal result remains fully readable without invented transcript blocks."""
    app, client, sid = world
    _transcript(app, sid)
    body = "Fetched source line.\n" * 300
    task = _task(app, sid, "completed", body=body)
    tool = _read(client, sid)
    block = tool["presentation"]["blocks"][0]
    ref = block["content_ref"]
    assert task.result is not None and ref["result_sha256"] == task.result.sha256
    path = f"/v1/sessions/{sid}/tools/{CALL}/presentation/{block['id']}"
    response = client.get(
        path, params={"cursor": ref["cursor"], "result_sha256": ref["result_sha256"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == body[ref["cursor"] : ref["cursor"] + 2048]
    assert (
        client.get(path, params={"cursor": ref["cursor"], "result_sha256": "changed"}).status_code
        == 409
    )


def test_cancellation_request_does_not_fabricate_terminal_cancellation(
    world: tuple[Any, TestClient, str],
) -> None:
    """Accepted cancellation intent remains distinct from physical settlement."""
    app, client, sid = world
    _transcript(app, sid)
    task = _task(app, sid, "running")
    app.state.task_history.record_snapshot(
        task.model_copy(update={"cancellation_requested_at": now()}), observation_id=uuid4().hex
    )
    tool = _read(client, sid)
    assert tool["state"] == "running"
    assert tool["execution_task"]["terminal_at"] is None
    assert "settlement is not yet confirmed" in tool["presentation"]["blocks"][0]["text"]


def test_cancelled_backend_task_does_not_require_a_tool_result_body(
    world: tuple[Any, TestClient, str],
) -> None:
    """A valid cancellation receipt settles cancellation without inventing output."""
    app, client, sid = world
    _transcript(app, sid)
    _task(app, sid, "cancelled")
    tool = _read(client, sid)
    assert tool["state"] == "cancelled"
    assert tool["output"] is None
    assert "cancelled" in tool["presentation"]["blocks"][0]["text"]


def test_receipt_and_task_custody_must_agree(world: tuple[Any, TestClient, str]) -> None:
    """A conflicting local receipt is uncertainty, not an unrelated task's output."""
    app, client, sid = world
    _transcript(app, sid)
    _task(app, sid, "completed")
    app.state.messages[sid][0].parts[1].structured_content["handle"] = "task_other"
    tool = _read(client, sid)
    assert tool["state"] == "unknown"
    assert tool["execution_problem"] == "submission_task_mismatch"
    assert "execution_task" not in tool


def test_completed_task_without_result_cannot_be_success(
    world: tuple[Any, TestClient, str],
) -> None:
    """A status label alone cannot establish readable successful execution."""
    app, client, sid = world
    _transcript(app, sid)
    _task(app, sid, "completed", include_result=False)
    tool = _read(client, sid)
    assert tool["state"] == "unknown"
    assert "no recorded terminal result" in tool["error"]


@pytest.mark.parametrize("failed", [False, True])
def test_task_backend_identity_selects_declared_web_presentation(
    world: tuple[Any, TestClient, str],
    failed: bool,
) -> None:
    """Unqualified backend names retain their declared server/tool semantics."""
    app, client, sid = world
    _transcript(app, sid)
    _task(
        app,
        sid,
        "failed" if failed else "completed",
        failed=failed,
        tool_name="fetch",
        body="Fetch limit exceeded" if failed else '{"content":"Source document"}',
        structured=None if failed else {"content": "Source document"},
    )
    tool = _read(client, sid)
    assert tool["state"] == ("failed" if failed else "succeeded")
    assert tool["presentation"]["action"] == "Fetch"
    expected = "Fetch limit exceeded" if failed else "Source document"
    assert any(block.get("text") == expected for block in tool["presentation"]["blocks"])
