"""Selective result reads keep exact source, paging and authorization semantics."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from clio_agent.arc.task_types import CoordinationGrant
from clio_agent.gact.native_work import now
from clio_agent.gact.result_selection import result_keys, select_fields, validate_selection
from clio_agent.gact.task_access import TaskAccessDenied, TaskCaller
from tests.test_gact.test_flow_tool_evidence import _tool
from tests.test_gact.test_permission_evidence import world as world

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.mark.parametrize("select", [[], ["plain"], ["/~"], ["/~2"], ["/a", "/a"], [3]])
def test_invalid_pointer_selection_is_explicit(select: Any) -> None:
    with pytest.raises(ValueError, match="JSON Pointers"):
        validate_selection(select)


def test_pointer_keys_lists_and_missing_values_are_distinct() -> None:
    value = {"a/b": {"~": [None, {"": "é🚀"}]}, "empty": None}
    assert select_fields(value, ["/a~1b/~0/1/", "/empty"]) == {
        "/a~1b/~0/1/": "é🚀",
        "/empty": None,
    }
    assert select_fields(value, [""]) == {"": value}
    for pointer in ("/missing", "/a~1b/~0/01", "/a~1b/~0/-", "/a~1b/~0/2"):
        with pytest.raises(ValueError, match="unavailable"):
            select_fields(value, [pointer])


def test_key_inventory_cannot_duplicate_a_large_result_key() -> None:
    assert result_keys({"small": 1, "large" * 1000: 2}) == {
        "result_keys": ["small"],
        "result_keys_truncated": True,
    }


def test_rest_reads_only_metadata_then_preserves_selected_unicode_paging(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    metadata = {"path": "D:/workspace/é-report.md", "status": "verified"}
    body = {"answer": "irrelevant long report" * 1000, "workflow_state": {"delivery": metadata}}
    task = _tool(app, root, body)
    path = f"/v1/sessions/{sid}/tasks/{task.handle}/result"
    pointer = "/workflow_state/delivery"
    response = client.get(path, params=[("select", pointer)])
    assert response.status_code == 200
    page = response.json()
    assert page["result"] == {pointer: metadata} and page["next_cursor"] is None
    assert "irrelevant" not in json.dumps(page)
    assert app.state.task_history.read_content(task.result) == body
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "21")
    page = client.get(path, params=[("select", pointer)]).json()
    reference, pieces = page["result_reference"], []
    cursor = page["next_cursor"]
    mismatch = client.get(path, params=[("cursor", cursor), ("select", "/answer")])
    assert mismatch.status_code == 404 and "different selection" in mismatch.text
    while True:
        assert page["result_selection"] == [pointer] and page["result_reference"] == reference
        pieces.append(page["result"]["preview"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        page = client.get(path, params={"cursor": cursor}).json()
    assert json.loads("".join(pieces)) == {pointer: metadata}
    missing = client.get(path, params=[("select", "/no_such_key")])
    assert missing.status_code == 404 and "unavailable" in missing.text
    assert client.get(path).json()["result_keys"] == list(body)


def test_selected_result_rechecks_real_grant_revocation_after_read(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, sid, root = request.getfixturevalue("world")
    reader = client.post("/v1/sessions", json={"title": "Reader"}).json()["id"]
    task = _tool(app, root, {"private": "authorized only before revocation"})
    history = app.state.task_history
    grant = CoordinationGrant(
        id="grant_" + uuid4().hex,
        granting_human=app.state.human_principal,
        recipient_session=reader,
        target_session=sid,
        target_scope="tree",
        rights=("read",),
        created_at=now(),
    )
    history.record_grant(grant, observation_id=uuid4().hex)
    read = history.read_content

    def revoke(reference: Any) -> Any:
        value = read(reference)
        if reference == task.result:
            history.record_grant(
                grant.model_copy(update={"revoked_at": now()}), observation_id=uuid4().hex
            )
        return value

    monkeypatch.setattr(history, "read_content", revoke)
    with pytest.raises(TaskAccessDenied):
        app.state.task_operations.result(
            TaskCaller(reader, app.state.human_principal), task.handle, select=["/private"]
        )
