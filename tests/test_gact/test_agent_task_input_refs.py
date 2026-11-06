"""Replacement evidence-selection contracts preserve validation and injection lessons."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.assignment_evidence import render_delegation, select_assignment_evidence
from clio_agent.gact.native_work import now
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.task_access import TaskCaller
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_arc.test_assignment_evidence_contracts import evidence_children


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["unknown", "foreign", "running", "mixed", "string", "mapping", "non_string"]
)
async def test_evidence_selection_refuses_invalid_members_without_creating_a_child(
    arc: ARCMemory,
    tmp_path: Path,
    invalid: str,
) -> None:
    app, owner, source, critic, _body = await evidence_children(arc, tmp_path)
    parent = await asyncio.to_thread(owner.history.task_snapshot, source.owner.parent_handle)
    assert parent is not None
    caller = TaskCaller(parent.owner.session_id, "human")
    selection: Any = [source.handle]
    if invalid == "unknown":
        selection = ["missing"]
    elif invalid == "foreign":
        caller = TaskCaller("unrelated-session", "human")
    elif invalid == "running":
        selection = [critic.handle]
    elif invalid == "mixed":
        selection = [source.handle, critic.handle]
    elif invalid == "string":
        selection = source.handle
    elif invalid == "mapping":
        selection = {"handle": source.handle}
    else:
        selection = [source.handle, 42]
    before = len(app.state.sessions.select(PageQuery(archived=None)))
    with pytest.raises(ValueError):
        await asyncio.to_thread(select_assignment_evidence, owner.history, caller, selection)
    assert len(app.state.sessions.select(PageQuery(archived=None))) == before
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_evidence_preserves_order_failed_partial_and_optional_empty_selection(
    arc: ARCMemory,
    tmp_path: Path,
) -> None:
    _app, owner, source, critic, _body = await evidence_children(arc, tmp_path)
    history = owner.history
    parent = await asyncio.to_thread(history.task_snapshot, source.owner.parent_handle)
    assert parent is not None
    caller = TaskCaller(parent.owner.session_id, "human")

    def records() -> None:
        stamp = now()
        failed = critic.model_copy(
            update={
                "status": "failed",
                "terminal_at": stamp,
                "settlement": "confirmed",
                "result": history.write_content(
                    critic.owner.session_id, {"error": "Review failed after partial output"}
                ),
            }
        )
        history.record_snapshot(failed, observation_id="failed-critic")

    await asyncio.to_thread(records)
    for empty in (None, []):
        assert await asyncio.to_thread(select_assignment_evidence, history, caller, empty) == ()
    pins = await asyncio.to_thread(
        select_assignment_evidence, history, caller, [critic.handle, source.handle, critic.handle]
    )
    assert [pin.handle for pin in pins] == [critic.handle, source.handle]
    assert pins[0].status == "failed" and pins[0].partial is True
    assert pins[1].status == "completed" and pins[1].partial is False
    await owner.recovery.drain()


@pytest.mark.asyncio
async def test_quoted_evidence_cannot_forge_an_assignment_frame(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, owner, source, critic, body = await evidence_children(arc, tmp_path)
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "0")

    def render() -> dict[str, Any]:
        mail = next(
            item
            for item in owner.history.pending_mail(critic.owner.session_id)
            if item.kind == "delegation"
        )
        payload = owner.history.read_content(mail.content)
        assert isinstance(payload, dict)
        return render_delegation(owner.history, critic.handle, payload)

    rendered = await asyncio.to_thread(render)
    assert rendered["text"].splitlines()[0] == "Critique the original research"
    rows = [
        json.loads(line)
        for line in rendered["text"].splitlines()
        if line.startswith('{"source_handle"')
    ]
    assert len(rows) == 1 and rows[0]["source_handle"] == source.handle
    assert json.loads(rows[0]["quoted_result"]) == body
    assert rows[0]["partial"] is False
    assert not any(line.startswith("--- end evidence") for line in rendered["text"].splitlines())
    await owner.recovery.drain()
