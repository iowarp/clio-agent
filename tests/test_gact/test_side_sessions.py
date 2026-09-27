"""Read-only side sessions: a /btw-style aside that can read, never write.

"More details" on a selected passage opens a side conversation with the same
context as its parent. It answers questions about the conversation without
changing it: its agent may call only tools that DECLARE no side effects (MCP
``readOnlyHint`` without ``destructiveHint``, or a native catalog ``read`` tag);
every other tool, including one with no annotations at all, is denied with a
typed reason, whatever the approval mode or user policy says. Nothing it does
lands in the parent's transcript.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _tool_session_context, build_app
from clio_agent.gact.permission_gate import _external_mcp_permission_context, _make_permission_gate
from clio_agent.gact.side_sessions import (
    READ_ONLY_SIDE_PROFILE,
    REASON_READ_ONLY_SIDE_SESSION,
)
from clio_agent.gact.types import Message, Part

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _parent_with_history(client: TestClient, app) -> str:
    sid = client.post(
        "/v1/sessions", json={"title": "Dinner ideas", "approval_mode": "bypass"}
    ).json()["id"]
    app.state.messages[sid] = [
        Message(
            id="msg_user",
            session_id=sid,
            role="user",
            created_at="2026-09-27T10:00:00Z",
            updated_at="2026-09-27T10:00:00Z",
            parts=[Part(type="text", text="Suggest a dinner.")],
        ),
        Message(
            id="msg_answer",
            session_id=sid,
            role="assistant",
            created_at="2026-09-27T10:00:05Z",
            updated_at="2026-09-27T10:00:05Z",
            parts=[Part(type="text", text="Salmon cakes with a brighter, tangy dill sauce.")],
        ),
    ]
    app.state.sessions.update(sid, message_count=2)
    return sid


def _open_side(client: TestClient, sid: str, **selection: str):
    return client.post(
        f"/v1/sessions/{sid}/side-sessions",
        json={"selection": {"text": "brighter, tangy", "message_id": "msg_answer", **selection}},
    )


# --------------------------------------------------------------------------- #
# The side session: same context, separate, read-only, ephemeral
# --------------------------------------------------------------------------- #


def test_side_session_carries_the_parent_context_in_a_separate_session(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent = _parent_with_history(client, app)
        resp = _open_side(client, parent)
        assert resp.status_code == 201, resp.text
        side = resp.json()

        assert side["id"] != parent
        assert side["parent_session_id"] == parent
        assert side["metadata"]["side_session"] == {
            "parent_session_id": parent,
            "read_only": True,
            "ephemeral": True,
            "selection": {"text": "brighter, tangy", "message_id": "msg_answer"},
        }
        # Same context: the parent's history, copied (not aliased).
        copied = app.state.messages[side["id"]]
        assert [m.id for m in copied] == ["msg_user", "msg_answer"]
        assert copied[1] is not app.state.messages[parent][1]
        # Server-owned read-only posture; the public approval mode does not grant bypass.
        stored = app.state.sessions.get(side["id"])
        assert stored.approval_profile == READ_ONLY_SIDE_PROFILE
        assert side["approval_mode"] == "ask"
        # The parent is untouched.
        assert [m.id for m in app.state.messages[parent]] == ["msg_user", "msg_answer"]


def test_opening_a_new_side_session_retires_the_previous_one(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent = _parent_with_history(client, app)
        first = _open_side(client, parent).json()["id"]
        second = _open_side(client, parent).json()["id"]

        assert app.state.sessions.get(first) is None
        assert app.state.sessions.get(second) is not None


def test_side_session_rejects_an_unknown_parent_and_a_side_parent(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        assert _open_side(client, "sess_missing").status_code == 404
        parent = _parent_with_history(client, app)
        side = _open_side(client, parent).json()["id"]
        nested = _open_side(client, side)
        assert nested.status_code == 422
        assert nested.json()["error"]["error"] == "side_session_nested"


def test_public_session_routes_cannot_mint_the_read_only_profile(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_profile": READ_ONLY_SIDE_PROFILE}
        ).json()["id"]
        assert app.state.sessions.get(sid).approval_profile == ""


# --------------------------------------------------------------------------- #
# The read-only tool policy
# --------------------------------------------------------------------------- #


def test_side_session_allows_declared_read_only_tools(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        side = _open_side(client, _parent_with_history(client, app)).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(side):
            assert gate("fs_read_file", {"filepath": "x"}) == "allow"
            assert (
                gate(
                    "remote.lookup",
                    {},
                    _external_mcp_permission_context(
                        {"readOnlyHint": True, "destructiveHint": False}
                    ),
                )
                == "allow"
            )


@pytest.mark.parametrize(
    ("tool", "context"),
    [
        # Native tool whose catalog annotations declare a write.
        ("fs_apply_edit_write", None),
        # Effectful native tool (open world, no read tag).
        ("shell_bash", None),
        # External MCP tool that declares a side effect.
        ("remote.submit", {"readOnlyHint": False}),
        # External MCP tool that claims read-only but is also destructive.
        ("remote.purge", {"readOnlyHint": True, "destructiveHint": True}),
        # No annotations at all: denied, never guessed.
        ("remote.unknown", {}),
        ("hdf5_list_datasets", None),
    ],
)
def test_side_session_denies_every_tool_that_does_not_declare_no_side_effects(
    tmp_path: Path, tool: str, context: dict | None
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        # The parent runs in bypass; that posture must not leak into the aside.
        side = _open_side(client, _parent_with_history(client, app)).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(side):
            decision = gate(
                tool,
                {"cmd": "echo hi"},
                _external_mcp_permission_context(context) if context is not None else None,
            )

        assert decision == "deny"
        assert "read-only side conversation" in getattr(decision, "deny_message", "")
        rows = [row for row in app.state.permissions.values() if row["session_id"] == side]
        assert len(rows) == 1
        assert rows[0]["status"] == "auto_denied"
        assert rows[0]["reason"] == REASON_READ_ONLY_SIDE_SESSION


def test_a_user_allow_policy_cannot_widen_a_side_session(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        side = _open_side(client, _parent_with_history(client, app)).json()["id"]
        client.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": side,
                        "tool_name_pattern": "*",
                        "action": "allow",
                    }
                ]
            },
        )
        gate = _make_permission_gate(app)
        with _tool_session_context(side):
            assert gate("shell_bash", {"cmd": "rm -rf /"}) == "deny"


def test_the_parent_keeps_its_own_posture(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent = _parent_with_history(client, app)
        _open_side(client, parent)
        gate = _make_permission_gate(app)
        with _tool_session_context(parent):
            assert gate("shell_bash", {"cmd": "echo ok"}) == "allow"


# --------------------------------------------------------------------------- #
# Native tools that never reach the gate
# --------------------------------------------------------------------------- #


def _native(name: str, *, read_only: bool = False):
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    def fn() -> str:
        """A native test tool."""
        return name

    return native_tool(
        fn,
        name=name,
        desc="test",
        args={},
        presentation="text",
        domain="workspace",
        read_only=read_only,
    )


def _mcp_bridged(name: str):
    import dspy

    from clio_agent.tools.execution import TOOL_OBSERVED_ATTR

    def fn(**_: object) -> str:
        return name

    setattr(fn, TOOL_OBSERVED_ATTR, True)
    return dspy.Tool(func=fn, name=name, desc="bridged", args={})


def _names(tools) -> list[str]:
    return sorted(tool.name for tool in tools)


def test_side_session_toolset_keeps_only_declared_read_only_natives(tmp_path: Path) -> None:
    from clio_agent.gact import context as gact_context
    from clio_agent.gact.agents.tool_instrumentation import instrument_tools

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent = _parent_with_history(client, app)
        side = _open_side(client, parent).json()["id"]
        tools = [
            _native("spawn_agent_task"),  # effectful native: never reaches the gate
            _native("raise_alert_card"),
            _native("workspace_resource_read", read_only=True),
            _mcp_bridged("fs_apply_edit_write"),  # gated per call instead
        ]
        app_token = gact_context.set_app(app)
        try:
            session_token = gact_context.set_session_id(side)
            try:
                side_tools = instrument_tools(tools)
            finally:
                gact_context.reset(session_token)
            parent_token = gact_context.set_session_id(parent)
            try:
                parent_tools = instrument_tools(tools)
            finally:
                gact_context.reset(parent_token)
        finally:
            gact_context.reset(app_token)

    assert _names(side_tools) == ["fs_apply_edit_write", "workspace_resource_read"]
    assert _names(parent_tools) == [
        "fs_apply_edit_write",
        "raise_alert_card",
        "spawn_agent_task",
        "workspace_resource_read",
    ]


def test_natives_default_to_effectful_and_the_read_only_set_is_declared() -> None:
    from clio_agent.gact.agents.tool_instrumentation import READ_ONLY_ATTR
    from clio_agent.gact.goal import build_goal_status_tool

    assert getattr(_native("undeclared").func, READ_ONLY_ATTR) is False
    assert getattr(build_goal_status_tool().func, READ_ONLY_ATTR) is True


def test_proposed_edits_cannot_be_applied_from_a_side_session(tmp_path: Path) -> None:
    """A read-tagged proposal tool may draft an edit; applying it is a write, refused."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        side = _open_side(client, _parent_with_history(client, app)).json()["id"]
        app.state.pending_diffs[side] = [
            {"path": "notes.txt", "status": "pending", "new_content": "fajitas\n"}
        ]
        resp = client.post(f"/v1/sessions/{side}/diffs/apply", json={})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "side_session_read_only"
    assert app.state.pending_diffs[side][0]["status"] == "pending"
