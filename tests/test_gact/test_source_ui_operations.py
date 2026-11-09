"""Folder link/refresh/download started from the UI never run the agent on their own.

The person's next message runs instead, and the session facts tell the agent which
operation is still in flight and which handle to wait on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
from clio_agent.gact.agents import session_inventory
from clio_agent.gact.agents.native_presenters_inspection import source_opened
from clio_agent.gact.native_work import now
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_publication import (
    HUMAN_SOURCE_INVOCATION,
    publish_task_notice,
    task_owner_recipient,
)


def _indexing(history: TaskHistory, handle: str, invocation_id: str) -> TaskSnapshot:
    stamp = now()
    task = TaskSnapshot(
        handle=handle,
        kind="Indexing",
        owner=TaskOwner(human_id="human", session_id="owner", assignment_id="assignment"),
        label="Index Calls",
        tool="connected_data_index",
        invocation_id=invocation_id,
        created_at=stamp,
        updated_at=stamp,
        status="completed",
        raw_status="completed",
        settlement="confirmed",
        terminal_at=stamp,
        result=history.write_content("owner", {"entries": 5}),
    )
    history.record_snapshot(task, observation_id=handle + "-done")
    return task


@pytest.mark.parametrize(
    ("invocation_id", "wakes"),
    [(HUMAN_SOURCE_INVOCATION + "link", False), ("agent-call", True)],
)
def test_only_agent_started_source_operations_notify_their_session(
    arc: ARCMemory, invocation_id: str, wakes: bool
) -> None:
    """A UI index finishing records no eligible task_result: nothing admits a turn."""
    history = TaskHistory(arc)
    handle = "task_index_" + ("agent" if wakes else "ui")
    with history.serialized():
        task = _indexing(history, handle, invocation_id)
        publish_task_notice(history, task)
    assert (task_owner_recipient(history, task) == "owner") is wakes
    notice = history.mail_item("owner", f"outcome:{handle}:owner")
    assert (notice is not None) is wakes
    if notice is not None:
        assert notice.delivery == "eligible"


def _app_with_source(monkeypatch: pytest.MonkeyPatch, views: list[dict[str, Any]]) -> Any:
    source = SimpleNamespace(
        id="source_calls",
        label="Calls",
        provider="drive",
        local_path=None,
        owner=SimpleNamespace(clio_id="clio"),
    )
    record = SimpleNamespace(source=source, connected=True, linked_access="read_only")
    service = SimpleNamespace(
        sources=lambda _workspace: [record], store=SimpleNamespace(clio_id="clio")
    )
    monkeypatch.setattr(
        "clio_agent.gact.storage.task_controls.source_operation_views",
        lambda _app, source_id: views if source_id == "source_calls" else [],
    )
    return SimpleNamespace(state=SimpleNamespace(connected_storage=service, task_history=object()))


def test_session_facts_name_a_running_index_and_the_handle_to_wait_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_source(
        monkeypatch,
        [
            {"task_handle": "task_old", "kind": "indexing", "state": "completed"},
            {"task_handle": "task_link", "kind": "indexing", "state": "running"},
            {"id": "op_retained", "kind": "materialize", "state": "running"},
        ],
    )
    facts = session_inventory.source_facts(app, "workspace")
    assert facts[0]["operations"] == [
        {"handle": "task_link", "kind": "indexing", "state": "running"}
    ]
    text = injections.render("session_facts_sources", {"sources": facts})
    assert "indexing of Calls is still running (task task_link)" in text
    assert "wait_tasks" in text


def test_session_facts_without_running_operations_say_nothing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app_with_source(
        monkeypatch, [{"task_handle": "task_old", "kind": "indexing", "state": "completed"}]
    )
    facts = session_inventory.source_facts(app, "workspace")
    assert facts[0]["operations"] == []
    text = injections.render("session_facts_sources", {"sources": facts})
    assert "still" not in text and "wait_tasks" not in text


def test_folder_listing_presents_an_unknown_size_instead_of_zero_bytes() -> None:
    view = source_opened(
        {},
        {
            "source_id": "source_calls",
            "folder": "Calls",
            "entries": [
                {"path": "Meeting.pdf", "kind": "file", "size": None},
                {"path": "notes.txt", "kind": "file", "size": 12},
            ],
        },
    )
    text = repr(view)
    assert "Bytes: unknown until read" in text
    assert "Bytes: 12" in text
    assert "Bytes: 0" not in text and "Bytes: None" not in text
