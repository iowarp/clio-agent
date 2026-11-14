"""A built-in that acts outside CLIO follows the session's approval mode (apps ledger, PM).

The person's rule: in ``ask`` every such call asks, with allow once, for this session or
for this workspace (so once per tool); in ``bypass`` nothing asks. A view's own tools
(``app__*``) and ``set_connector_value`` declare ``native_tool(outside=True)``; a view tool
annotated ``readOnlyHint`` is exempt, as an MCP tool's read is. CLIO's bookkeeping
built-ins (``search_tools``, ``load_skill``) declare nothing and keep running unasked.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool
from clio_agent.gact.agents.tool_instrumentation import acts_outside, native_tool
from clio_agent.gact.agents.tool_recording import recording_tool
from clio_agent.gact.code_mode import grants
from clio_agent.gact.connector_tool import build_set_connector_value_tool
from clio_agent.gact.mcp_app_tools import AppTool, _agent_tool
from clio_agent.gact.runtime import tool_permission_layers as layers
from tests.turn_signals import TURN_SIGNAL_BACKSTOP_S


@pytest.fixture(autouse=True)
def _fresh_conf(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CLIO_TOOLS_PERMISSIONS", raising=False)
    conf.reload()
    yield
    conf.reload()


def _view_tool(read_only: bool = False, name: str = "move_card") -> Any:
    return _agent_tool(
        AppTool(
            agent_name=f"app__board__{name}",
            server="board",
            app_instance_id="app_1",
            name=name,
            description="Move a card to another column.",
            input_schema={"type": "object", "properties": {"card": {"type": "string"}}},
            opening_tool="board_open",
            read_only=read_only,
        )
    )


def _bookkeeping() -> Any:
    def search_notes(query: str) -> str:
        return query

    return native_tool(
        search_notes,
        name="search_notes",
        desc="Find notes.",
        args={},
        presentation="todos",
        domain="tasks",
    )


def _session(client: Any, mode: str, workspace: str = "") -> str:
    body: dict[str, Any] = {"title": "t", "approval_mode": mode}
    if workspace:
        body["workspace_id"] = workspace
    return client.post("/v1/sessions", json=body).json()["id"]


def _pending(app: Any) -> dict[str, Any]:
    deadline = time.time() + TURN_SIGNAL_BACKSTOP_S
    while time.time() < deadline:
        rows = [r for r in app.state.permissions.values() if r["status"] == "pending"]
        if rows:
            return rows[0]
        time.sleep(0.02)
    raise AssertionError("no permission question appeared")


def _call(app: Any, sid: str, tool: Any, name: str, grant: Any = None) -> dict[str, Any]:
    """Run ``native_gate`` for one call in a thread; what it yielded."""
    result: dict[str, Any] = {}

    def fire() -> None:
        tokens = _ctx.set_app(app), _ctx.set_session_id(sid)
        bound = grants.bind_grant(grant) if grant is not None else None
        try:
            with layers.native_gate(tool, name, {"card": "c1"}) as refused:
                result["refused"] = refused
                result["answered"] = layers.answered(name)
        finally:
            if bound is not None:
                grants.reset_grant(bound)
            _ctx.reset(tokens[1])
            _ctx.reset(tokens[0])

    thread = threading.Thread(target=fire)
    thread.start()
    result["thread"] = thread
    return result


def _settle(result: dict[str, Any]) -> dict[str, Any]:
    result.pop("thread").join(timeout=5)
    return result


def _answer(client: Any, app: Any, result: dict[str, Any], action: str) -> dict[str, Any]:
    row = _pending(app)
    client.post(f"/v1/permissions/{row['id']}", json={"action": action})
    _settle(result)
    return row


def test_the_declaration_marks_view_tools_and_set_connector_value() -> None:
    assert acts_outside(_view_tool())
    assert not acts_outside(_view_tool(read_only=True))  # readOnlyHint: exempt
    assert acts_outside(build_set_connector_value_tool())
    # The agent's recorded wrapper keeps the declaration (live: it was lost there first).
    assert acts_outside(recording_tool(build_set_connector_value_tool()))
    assert not acts_outside(_bookkeeping())
    assert not acts_outside(ClioNativeTool(lambda: "x", name="plain", desc="Plain."))


@pytest.mark.usefixtures("host_agent_executor")
class TestOutsideToolsFollowTheMode:
    def test_ask_asks_by_title_and_this_session_stops_asking(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        tool, name = _view_tool(), "app__board__move_card"
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "ask")
            first = _call(app, sid, tool, name)
            row = _answer(client, app, first, "allow_session")
            assert row["summary"] == "Move card"  # the tool's title, not its identifier
            assert row["tool_call"]["tool_name"] == name
            assert first == {"refused": "", "answered": True}
            again = _settle(_call(app, sid, tool, name))  # no question this time
            assert again == {"refused": "", "answered": True}
            assert [r["status"] for r in app.state.permissions.values()].count("pending") == 0
            # A new session in the same workspace asks again.
            other = _session(client, "ask")
            fresh = _call(app, other, tool, name)
            _answer(client, app, fresh, "deny")
            assert "was not run" in fresh["refused"]

    def test_this_workspace_carries_across_sessions(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        tool, name = build_set_connector_value_tool(), "set_connector_value"
        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "ask")
            row = _answer(client, app, _call(app, sid, tool, name), "allow_workspace")
            assert row["summary"] == "Set connector value"
            other = _session(client, "ask")
            assert _settle(_call(app, other, tool, name))["refused"] == ""
            policies = app.state.permission_policies
            assert any(
                p.get("scope") == "workspace" and p.get("tool_name_pattern") == name
                for p in policies
            )

    def test_bypass_never_asks_and_records_why(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app
        from clio_agent.gact.permission_gate import REASON_APPROVAL_BYPASS

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "bypass")
            for tool, name in (
                (_view_tool(), "app__board__move_card"),
                (build_set_connector_value_tool(), "set_connector_value"),
            ):
                assert _settle(_call(app, sid, tool, name))["refused"] == ""
            reasons = [r.get("reason") for r in app.state.permissions.values()]
            assert reasons == [REASON_APPROVAL_BYPASS, REASON_APPROVAL_BYPASS]

    def test_a_deny_rule_holds_in_bypass(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "bypass")
            app.state.permission_policies = [
                {"scope": "workspace", "tool_name_pattern": "app__board__*", "action": "deny"}
            ]
            refused = _settle(_call(app, sid, _view_tool(), "app__board__move_card"))
            assert "was not run" in refused["refused"]

    def test_read_only_view_tools_and_bookkeeping_do_not_ask(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "ask")
            looked = _settle(
                _call(app, sid, _view_tool(True, "list_cards"), "app__board__list_cards")
            )
            searched = _settle(_call(app, sid, _bookkeeping(), "search_notes"))
            assert looked["refused"] == "" and searched["refused"] == ""
            assert not app.state.permissions  # no question, nothing to record

    def test_a_script_keeps_its_accepted_mode(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from clio_agent.gact.app import build_app

        app = build_app(sessions_path=tmp_path / "s.json")
        with TestClient(app) as client:
            sid = _session(client, "ask")
            grant = grants.ScriptGrant("call_1", "await tools.x()", approval_mode="bypass")
            done = _settle(_call(app, sid, _view_tool(), "app__board__move_card", grant))
            assert done["refused"] == ""
            assert not [r for r in app.state.permissions.values() if r["status"] == "pending"]


@pytest.mark.usefixtures("host_agent_executor")
def test_bookkeeping_defaults_read_the_scripts_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default ``ask`` on a bookkeeping built-in is skipped under a script approved in
    bypass, as the gate does, although the session is in ``ask`` now (finding 2)."""
    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app

    monkeypatch.setenv("CLIO_TOOLS_PERMISSIONS", json.dumps({"search_notes": "ask"}))
    conf.reload()
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, "ask")
        grant = grants.ScriptGrant("call_1", "await tools.x()", approval_mode="bypass")
        done = _settle(_call(app, sid, _bookkeeping(), "search_notes", grant))
        assert done["refused"] == ""
        # Without the script, the session's ask mode keeps the default's question.
        asked = _call(app, sid, _bookkeeping(), "search_notes")
        row = _answer(client, app, asked, "allow")
        assert row["summary"] == "Use search_notes"  # untitled: the plain line
        assert asked["refused"] == ""


def test_no_session_runs_as_before() -> None:
    with layers.native_gate(_view_tool(), "app__board__move_card", {}) as refused:
        assert refused == ""
