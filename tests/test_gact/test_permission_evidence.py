"""Permission history must preserve attribution, timeouts and checkpointed views."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.flows.context import FlowContext
from clio_agent.gact import permission_timeout
from clio_agent.gact.app import build_app
from clio_agent.gact.permission_evidence import permission_identity, permission_receipts
from clio_agent.gact.permission_gate import _make_permission_gate, resolve_permission
from clio_agent.gact.permission_timeout import expire_permission
from clio_agent.gact.task_history import TaskHistory
from clio_agent.sdk import Flow
from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class ReceiptFlow(Flow):
    """A test flow that reads historical decisions, without running a model."""

    id = "receipt-test"

    async def forward(self, task: Any, ctx: FlowContext) -> Any:
        return await ctx.permission_receipts()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Any, TestClient, str, Any]]:
    """Use actual task/ARC writers and the production permission resolver."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Flow"}).json()["id"]
        task = record_assignment_fixture(app, sid)
        yield app, client, sid, task


def _request(app: Any, sid: str, pid: str, tool: str = "web_fetch") -> dict[str, Any]:
    with active_turn_fixture(app, sid):
        identity = permission_identity(app, sid)
    row = {
        "id": pid,
        "session_id": sid,
        **identity,
        "kind": "external_mcp",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_call": {"tool_name": tool, "input": {"token": "do-not-copy-this-secret"}},
    }
    app.state.permissions[pid] = row
    return row


def test_permission_history_retains_timeout_then_human_grant_and_linked_refresh(world: Any) -> None:
    app, _client, sid, root = world
    expired = _request(app, sid, "perm_expired")
    assert expire_permission(app, "perm_expired", expired, tool_name="web_fetch") == "deny"
    source = _request(app, sid, "perm_human")
    refreshed = _request(app, sid, "perm_refreshed")
    resolve_permission(app, source["id"], "allow_workspace")
    assert refreshed["policy_refresh_source"] == source["id"]
    app.state.permissions.clear()  # The bounded in-memory ledger is not our evidence source.
    evidence = permission_receipts(app, sid, root.handle)
    rows = {row["permission_id"]: row for row in evidence["receipts"]}
    assert rows["perm_expired"]["action"] == "deny"
    assert rows["perm_expired"]["decision_status"] == "timeout"
    assert rows["perm_expired"]["grantor"] == "system"
    assert rows["perm_human"]["grantor"] == "user"
    assert rows["perm_human"]["policy_scope"] == "workspace"
    assert rows["perm_refreshed"]["grantor"] == "system"
    assert rows["perm_refreshed"]["policy_refresh_source"] == "perm_human"
    assert "do-not-copy-this-secret" not in json.dumps(evidence)
    assert len({row["receipt_id"] for row in rows.values()}) == 3


def test_real_gate_captures_the_admitted_assignment_before_its_request_expires(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, sid, root = world
    monkeypatch.setattr(permission_timeout, "PERMISSION_REQUEST_TIMEOUT_S", 0.02)
    with active_turn_fixture(app, sid):
        decision = _make_permission_gate(app)(
            "external_fetch",
            {"token": "do-not-copy-this-secret"},
            context={"kind": "external_mcp", "annotations": {}},
        )
    assert decision == "deny"
    (receipt,) = permission_receipts(app, sid, root.handle)["receipts"]
    assert receipt["assignment_id"] == root.handle
    assert receipt["decision_status"] == "timeout"
    assert "do-not-copy-this-secret" not in json.dumps(receipt)


def test_scope_is_assignment_lineage_not_every_assignment_in_a_session(world: Any) -> None:
    app, client, sid, root = world
    child_sid = client.post("/v1/sessions", json={"title": "Child"}).json()["id"]
    child = record_assignment_fixture(app, child_sid, parent_sid=sid)
    request = _request(app, child_sid, "perm_child")
    resolve_permission(app, request["id"], "allow")
    sibling = record_assignment_fixture(app, sid)
    sibling_request = _request(app, sid, "perm_sibling")
    resolve_permission(app, sibling_request["id"], "allow_workspace")
    assert app.state.task_history.descendant_handles(root.handle) == {root.handle, child.handle}
    assert sibling.handle not in app.state.task_history.descendant_handles(root.handle)
    restored = TaskHistory(app.state.arc)
    assert restored.descendant_handles(root.handle) == {root.handle, child.handle}
    assert [
        row["permission_id"] for row in permission_receipts(app, sid, root.handle)["receipts"]
    ] == ["perm_child"]
    with pytest.raises(ValueError, match="own confirmed assignment"):
        permission_receipts(app, child_sid, root.handle)


def test_receipt_snapshots_replay_without_reinterpreting_later_decisions(world: Any) -> None:
    app, _client, sid, task = world

    def context() -> FlowContext:
        return FlowContext(
            app=app,
            session_id=sid,
            module=SimpleNamespace(resolution=None),
            flow=ReceiptFlow(),
            root_handle=task.handle,
            run_invocation="receipts",
            task="Read historical consent",
        )

    first = context()
    before = asyncio.run(first.permission_receipts())
    request = _request(app, sid, "perm_later")
    resolve_permission(app, request["id"], "allow_workspace")
    after = asyncio.run(first.permission_receipts())
    assert before["receipts"] == [] and len(after["receipts"]) == 1
    replay = context()
    assert asyncio.run(replay.permission_receipts()) == before
    assert asyncio.run(replay.permission_receipts()) == after
    assert asyncio.run(replay.permission_receipts(refresh=True)) == after
    after["receipts"].clear()
    replay2 = context()
    assert asyncio.run(replay2.permission_receipts()) == before
    assert len(asyncio.run(replay2.permission_receipts())["receipts"]) == 1
    denied = _request(app, sid, "perm_new_denial")
    resolve_permission(app, denied["id"], "deny")
    refreshed = asyncio.run(replay2.permission_receipts(refresh=True))
    assert len(refreshed["receipts"]) == 2
    assert after["receipts"] == []  # Mutating a returned value never edits its recorded snapshot.


def test_unattributed_legacy_decisions_are_not_guessed_from_a_reused_session(world: Any) -> None:
    app, _client, sid, root = world
    request = _request(app, sid, "perm_legacy")
    request.pop("assignment_id")
    resolve_permission(app, request["id"], "allow_workspace")
    evidence = permission_receipts(app, sid, root.handle)
    assert evidence["receipts"] == []
    assert evidence["coverage"] == "decisions_with_confirmed_assignment_identity"


def test_missing_durable_log_is_explicitly_unavailable(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _client, sid, root = world
    with monkeypatch.context() as patch:
        patch.setattr(app.state.task_history.arc, "_live", None)
        evidence = permission_receipts(app, sid, root.handle)
    assert evidence["status"] == "unavailable" and evidence["receipts"] == []
