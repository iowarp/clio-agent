"""The builder's session tools: records of named sessions, the /optimize scope and watermark.

``session_record`` reads only sessions the person selected (this conversation, ``@``
references or ids in the person's own words, the scope ``optimize_scope`` showed) and
refuses the rest with ``session_not_named``. ``optimize_scope`` finds the sessions a
plugin ran in, or every root conversation since the account's watermark;
``optimize_report_shown`` moves the watermark and keeps declined findings.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.app import build_app
from clio_agent.gact.builder import presentation
from clio_agent.gact.builder.tools import optimize_report_shown, optimize_scope, session_record
from clio_agent.gact.parts import Part
from clio_agent.gact.session_store import _append_session_message
from clio_agent.gact.types import Message

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CLIO_SCRATCH_ROOT", str(tmp_path / "scratch"))
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as started:
        yield started


def _say(app: Any, sid: str, n: int, role: str, *parts: Part) -> None:
    stamp = "2026-11-16T10:00:00Z"
    _append_session_message(
        app,
        sid,
        Message(
            id=f"msg_{sid[-6:]}_{n}",
            session_id=sid,
            role=role,
            created_at=stamp,
            updated_at=stamp,
            parts=list(parts),
        ),
    )


def _conversation(app: Any, title: str) -> Any:
    session = app.state.sessions.create(workspace_id="ws_global", title=title)
    _say(app, session.id, 1, "user", Part(id="u1", type="text", text=f"Work on {title}."))
    _say(
        app,
        session.id,
        2,
        "assistant",
        Part(
            id="c1",
            type="tool_call",
            call_id="call_1",
            tool_name="load_skill",
            input={"skill_id": "clio"},
        ),
        Part(id="a1", type="text", text="Done."),
    )
    return session


def _run(app: Any, sid: str, coro: Any) -> Any:
    app_token = context.set_app(app)
    sid_token = context.set_session_id(sid)
    try:
        return asyncio.run(coro)
    finally:
        context.reset(sid_token)
        context.reset(app_token)


def test_this_conversation_is_written_as_a_file_with_its_summary(client: TestClient) -> None:
    app = client.app
    here = _conversation(app, "HDF5 exploration")
    result = _run(app, here.id, session_record())
    assert result["written"] is True, result
    (row,) = result["records"]
    assert row["session_id"] == here.id and row["title"] == "HDF5 exploration"
    assert row["turns"] == 1 and row["messages"] == 2
    record = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
    assert record["export_schema"] == "clio.session-export.v2"
    assert record["session"]["id"] == here.id and "children" in record
    agent = json.loads(Path(row["agent_path"]).read_text(encoding="utf-8"))
    assert "guide" in {p["id"] for p in agent["plugins"]}
    shown = presentation.session_record({}, result, None)
    assert shown["summary"] == "HDF5 exploration"
    assert [b["target"] for b in shown["blocks"] if b["type"] == "link"] == ["session", "file"]


def test_an_unnamed_session_is_refused_and_nothing_is_written(client: TestClient) -> None:
    app = client.app
    here = _conversation(app, "here")
    other = _conversation(app, "someone else's")
    result = _run(app, here.id, session_record([other.id]))
    assert result == {
        "written": False,
        "reason": "session_not_named",
        "error": result["error"],
        "refused": [other.id],
    }
    shown = presentation.session_record({"sessions": [other.id]}, result, None)
    assert shown["status"] == "failed" and shown["summary"] == "Session not named"
    assert shown["blocks"][-1]["severity"] == "error"


def test_an_at_reference_or_an_id_the_person_wrote_names_a_session(client: TestClient) -> None:
    app = client.app
    first, second = _conversation(app, "first"), _conversation(app, "second")
    here = _conversation(app, "capture")
    _say(
        app,
        here.id,
        3,
        "user",
        Part(id="r1", type="context_ref", ref_kind="session", ref_id=first.id),
        Part(id="t1", type="text", text=f"and also {second.id}"),
    )
    result = _run(app, here.id, session_record([first.id, second.id]))
    assert result["written"] is True
    assert [r["session_id"] for r in result["records"]] == [first.id, second.id]


def test_a_plugin_scope_lists_the_sessions_it_ran_in_and_names_them(client: TestClient) -> None:
    app = client.app
    used = _conversation(app, "used the guide")
    plain = app.state.sessions.create(workspace_id="ws_global", title="plain")
    _say(app, plain.id, 1, "user", Part(id="u", type="text", text="hello"))
    here = app.state.sessions.create(workspace_id="ws_global", title="optimize")
    scope = _run(app, here.id, optimize_scope(plugin="guide"))
    assert scope["listed"] is True and scope["basis"] == "plugin_window"
    listed = {row["session_id"]: row["evidence"] for row in scope["sessions"]}
    assert listed == {used.id: ["loaded skill clio"]}
    # The shown scope is now named: its sessions can be read; others still cannot.
    assert _run(app, here.id, session_record([used.id]))["written"] is True
    assert _run(app, here.id, session_record([plain.id]))["reason"] == "session_not_named"
    shown = presentation.optimize_scope({"plugin": "guide"}, scope, None)
    assert shown["action"] == "Find the sessions guide ran in"
    assert shown["summary"] == "1 session"


def test_a_scan_child_reads_what_its_parent_s_scope_named(client: TestClient) -> None:
    app = client.app
    used = _conversation(app, "used")
    parent = app.state.sessions.create(workspace_id="ws_global", title="optimize")
    _run(app, parent.id, optimize_scope(plugin="guide"))
    child = app.state.sessions.create(
        workspace_id="ws_global", title="scan", parent_session_id=parent.id
    )
    assert _run(app, child.id, session_record([used.id]))["written"] is True


def test_global_scope_watermark_moves_only_when_the_report_is_shown(client: TestClient) -> None:
    app = client.app
    _conversation(app, "a week of work")
    here = app.state.sessions.create(workspace_id="ws_global", title="optimize")
    first = _run(app, here.id, optimize_scope())
    assert first["basis"] == "first_run" and first["count"] == 1
    again = _run(app, here.id, optimize_scope())
    assert again["basis"] == "first_run", "nothing moved before the report"
    declined = [{"key": "units", "plugin": "guide", "summary": "Units correction"}]
    shown = _run(app, here.id, optimize_report_shown(declined))
    assert shown["recorded"] and shown["watermark_moved"] and shown["declined_recorded"] == 1
    after = _run(app, here.id, optimize_scope())
    assert after["basis"] == "watermark" and after["count"] == 0
    assert after["declined"][0]["key"] == "units"
    view = presentation.optimize_report_shown({"declined": declined}, shown, None)
    assert view["summary"] == "Watermark moved, 1 finding declined"


def test_reporting_without_a_shown_scope_is_refused(client: TestClient) -> None:
    app = client.app
    here = app.state.sessions.create(workspace_id="ws_global", title="t")
    result = _run(app, here.id, optimize_report_shown([]))
    assert result["recorded"] is False and result["reason"] == "scope_not_shown"


def test_an_unknown_plugin_scope_is_typed(client: TestClient) -> None:
    app = client.app
    here = app.state.sessions.create(workspace_id="ws_global", title="t")
    result = _run(app, here.id, optimize_scope(plugin="no-such-plugin"))
    assert result["listed"] is False and result["reason"] == "plugin_not_installed"
