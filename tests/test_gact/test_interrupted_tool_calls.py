"""Turn recovery reports a call whose outcome was never recorded as unknown.

A read-only MCP call's task record is written behind its reply, and a call's
transcript part is sealed only at its result, so a process that stops mid-call can
leave the turn's ``tool.call.started`` event as the call's only record. The restart
closes the interrupted turn (``restart_interruptions``); the closing row must carry
that call with the typed reason ``tool_outcome_unrecorded``, and the transcript must
report its outcome as unknown: not as success, and not by leaving it out.

The "restart" is a second ``build_app`` over the same session registry and the same
clio-core store, as in ``test_transcript_file_switch``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact.interrupted_tool_calls import TOOL_OUTCOME_UNRECORDED
from clio_agent.gact.part_atoms import (
    append_part_atom,
    build_sealed_part_atom,
    message_stub,
    mint_message_part_atoms,
)
from clio_agent.gact.protocol.v3.tool_outcome import MISSING_EXECUTION_OUTCOME
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.types import Part

from .test_transcript_file_switch import _app, _message, _wire

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_TURN = "msg_user_crash"


@pytest.fixture
def file_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "false")


def _tool_event(arc: Any, sid: str, turn: str, event_type: str, call_id: str) -> None:
    payload: dict[str, Any] = {"call_id": call_id, "tool": "records.read"}
    if event_type == "tool.call.started":
        payload.update(args={"record_id": call_id}, started_at="2026-11-03T10:00:00+00:00")
    else:
        payload.update(result={"n": call_id}, completed_at="2026-11-03T10:00:01+00:00")
    arc.record_semantic_event(
        SemanticEvent(
            event_type=event_type,
            session_id=sid,
            turn_id=turn,
            trace_id="trace",
            span_id=f"{call_id}-{event_type}",
            status="running" if event_type == "tool.call.started" else "completed",
            subject={"call_id": call_id},
            actor={"tool": "records.read"},
            payload=payload,
        )
    )


def _sealed_call(arc: Any, sid: str, call_id: str) -> None:
    """The live ledger sealed this call's part at its result (#1337); no envelope yet."""
    stub = message_stub(
        message_id="msg_asst_crash", turn_id=_TURN, session_id=sid, created_at="2026-11-03"
    )
    part = Part(
        id=f"live_{call_id}_call",
        type="tool_call",
        call_id=call_id,
        tool_name="records.read",
        input={"record_id": call_id},
    )
    part.sequence = 1
    atom = build_sealed_part_atom(
        stub, part.model_dump(), 0, sealed_at=datetime.now(UTC).isoformat(), seal_source="live"
    )
    append_part_atom(arc._segments, sid, atom)


@pytest.mark.usefixtures("file_off")
@pytest.mark.parametrize("sealed", [False, True], ids=["no-sealed-part", "sealed-part"])
def test_a_started_call_with_no_recorded_outcome_is_reported_unknown_after_restart(
    tmp_path: Path, sealed: bool
) -> None:
    """The crashed turn: ``lost`` started (its read-only record never written), ``done``
    started and completed, and an earlier turn's ``earlier`` started with no completion.
    After the restart the closing row holds ``lost`` as an unknown, typed call; it does
    not invent ``done`` or the other turn's call, and a part the ledger sealed stays
    as it was.

    SABOTAGE: make ``unrecorded_tool_calls`` return ``[]`` and ``lost`` is missing from
    the transcript (a silent gap); drop the ``_ENDED`` check and ``done`` is added too;
    drop the incomplete-row branch of ``restart_interruptions._close_transcript`` and
    ``lost`` is missing when the turn's sealed part is the last row.
    """
    store = _MemoryStore()
    first = _app(tmp_path, store, "first")
    arc = first.state.arc
    sid = first.state.sessions.create(workspace_id="ws_default", title="crashed").id
    _tool_event(arc, sid, "msg_user_before", "tool.call.started", "earlier")
    mint_message_part_atoms(arc, sid, _message(sid, _TURN, "read the records"))
    _tool_event(arc, sid, _TURN, "tool.call.started", "done")
    _tool_event(arc, sid, _TURN, "tool.call.completed", "done")
    if sealed:
        _sealed_call(arc, sid, "done")
    _tool_event(arc, sid, _TURN, "tool.call.started", "lost")
    first.state.sessions.update(sid, status="running", message_count=1)

    second = _app(tmp_path, store, "second")
    assert second.state.restart_interruptions.waiting(sid)
    with TestClient(second) as client:
        rows = _wire(client, sid)
        response = client.get(f"/v1/sessions/{sid}/messages", headers={"X-GACT-Version": "0.3"})
        assert response.status_code == 200, response.text
        snapshot = response.json()
    assert not second.state.restart_interruptions.waiting(sid)
    assert [row["role"] for row in rows] == ["user", "assistant"]
    closing = rows[1]
    calls = {part["call_id"]: part for part in closing["parts"] if part["type"] == "tool_call"}
    assert set(calls) == ({"lost", "done"} if sealed else {"lost"})
    lost = calls["lost"]
    assert lost["tool_name"] == "records.read"
    assert lost["input"] == {"record_id": "lost"}
    assert lost["metadata"]["outcome"]["reason"] == TOOL_OUTCOME_UNRECORDED
    assert lost["metadata"]["started_at"] == "2026-11-03T10:00:00+00:00"
    if sealed:  # the turn's own incomplete row keeps its sealed part, verbatim, first
        assert closing["id"] == "msg_asst_crash" and closing["stop_reason"] == "incomplete"
        assert [part["call_id"] for part in closing["parts"]] == ["done", "lost"]
        assert "outcome" not in calls["done"].get("metadata", {})
    else:
        assert closing["error_info"]["error"] == "server_restart_interrupted"

    # What the client reads: the call's outcome is unknown, never a success or a gap.
    tools = {tool["id"]: tool for tool in snapshot["tools"]}
    assert tools["lost"]["state"] == "unknown"
    assert tools["lost"]["error"] == MISSING_EXECUTION_OUTCOME
    assert tools["lost"]["output"] is None
    assert "earlier" not in tools
    # The recovered row is durable: a third start reads it as recorded, adding nothing.
    third = _app(tmp_path, store, "third")
    with TestClient(third) as client:
        again = _wire(client, sid)
    assert [part["call_id"] for part in again[-1]["parts"] if part["type"] == "tool_call"] == [
        part["call_id"] for part in closing["parts"] if part["type"] == "tool_call"
    ]
