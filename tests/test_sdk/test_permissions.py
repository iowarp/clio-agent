"""Permission list/respond through the SDK (SPEC §6.11 + §4.7)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.sdk import ClioClient, NotFoundError
from tests.test_sdk.conftest import StreamingASGITransport, StubAgent, _fresh_arc

_PERMS = [
    {
        "tool_call": {
            "call_id": "c1",
            "tool_name": "shell.exec",
            "input": {"cmd": "rm -rf /tmp/scratch"},
        },
        "summary": "destructive shell command",
    }
]


@pytest.fixture()
def gated_client(tmp_path: Path) -> Any:
    from clio_agent.gact.app import build_app

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=StubAgent(permissions_requested=_PERMS),
        arc=_fresh_arc(tmp_path),
    )
    transport = StreamingASGITransport(app)
    with ClioClient("http://testserver", transport=transport) as client:
        yield client
    transport.close()


def _wait_for_pending(client: ClioClient, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = client.permissions.list(status="pending").permissions
        if rows:
            return rows[0]
        time.sleep(0.05)
    raise TimeoutError("no pending permission appeared")


def test_permission_requested_then_responded(gated_client: ClioClient) -> None:
    sess = gated_client.sessions.create(title="perm test")
    gated_client.messages.post(sess.id, text="please delete the scratch dir")

    pending = _wait_for_pending(gated_client)
    assert pending.status == "pending"
    assert pending.session_id == sess.id
    assert pending.tool_call.tool_name == "shell.exec"
    assert pending.tool_call.input == {"cmd": "rm -rf /tmp/scratch"}
    assert pending.summary == "destructive shell command"

    gated_client.permissions.respond(pending.id, "allow")

    assert gated_client.permissions.list(status="pending").permissions == []
    audit = gated_client.permissions.list(status="all")
    row = next(r for r in audit.permissions if r.id == pending.id)
    assert row.status == "resolved"
    assert row.action == "allow"
    assert row.resolved_at

    # Idempotent server-side: re-responding is a silent 204 (SPEC §6.11).
    gated_client.permissions.respond(pending.id, "allow")


def test_permission_list_filters_by_session(gated_client: ClioClient) -> None:
    sess = gated_client.sessions.create(title="filter test")
    gated_client.messages.post(sess.id, text="do the risky thing")
    _wait_for_pending(gated_client)

    listing = gated_client.permissions.list(session_id=sess.id, status="pending")
    assert len(listing.permissions) == 1
    assert listing.permissions[0].session_id == sess.id
    assert listing.metadata.get("session_id") == sess.id

    other = gated_client.permissions.list(session_id="sess_other", status="pending")
    assert other.permissions == []


def test_respond_unknown_permission_raises_not_found(client: ClioClient) -> None:
    with pytest.raises(NotFoundError):
        client.permissions.respond("perm_nope", "deny")
