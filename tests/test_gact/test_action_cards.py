"""Tests for the ``action_card`` part builder/emitter + the auto-attached
``raise_alert_card`` native tool (frozen wire contract, SPOTTER MVP week).

Covers:

* :func:`action_card_part` produces the frozen contract's wire shape verbatim
  (omit-empty, actions intact, unknown/unrelated fields absent).
* :func:`emit_action_card_part` makes the card VISIBLE through the real
  ``GET /v1/sessions/{sid}/messages`` surface — the consumer's semantics —
  whether or not the target session currently has an in-flight turn.
* :func:`build_raise_alert_card_tool` (the auto-attached native tool a
  spawned child calls) emits into its PARENT session with a working
  ``discuss`` handle, and returns the typed ``alert_card_no_parent`` error
  for a session with no live parent AgentTask — never a silent no-op.
* ``raise_alert_card`` reaches BOTH real module-assembly paths
  (``_build_blueprint_dspy_module``'s react branch AND
  ``_build_tool_user_agent_module``) exactly like ``create_artifact`` /
  ``load_skill`` — including for an expert that itself declares a curated
  ``tools:`` list, since ``_dynamic_agent_tools`` returns EXACTLY the
  requested tools and the auto-attached set is merged in SEPARATELY
  (``gact/agents/auto_tools.py``).
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.action_cards import (
    ALERT_CARD_NO_PARENT_ERROR,
    ALERT_CARD_PARENT_TRANSCRIPT_FROZEN,
    ALERT_CARD_TASK_ROW_MISSING_ERROR,
    action_card_part,
    build_raise_alert_card_tool,
    emit_action_card_part,
)
from clio_agent.gact.agent_tasks import STATUS_RUNNING, AgentTask
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef

_CONTRACT_ACTIONS = [
    {
        "id": "discuss",
        "label": "Discuss",
        "enabled": True,
        "behavior": {"kind": "focus_session", "handle_id": "task_xxxx"},
    },
    {
        "id": "address",
        "label": "Address",
        "enabled": False,
        "behavior": {"kind": "stub", "reason": "remediation lands in phase 2"},
    },
]


# --------------------------------------------------------------------------- #
# 1. builder produces the frozen contract's wire shape
# --------------------------------------------------------------------------- #


def test_action_card_part_wire_shape_matches_frozen_contract() -> None:
    part = action_card_part(
        source="spotter-ai",
        severity="critical",
        title="SPOTTER AI has detected an issue",
        body="run-012 anomalous (mean_biomass z=6.1). Campaign quarantined.",
        actions=_CONTRACT_ACTIONS,
    )
    wire = part.to_wire()

    assert wire["type"] == "action_card"
    assert wire["source"] == "spotter-ai"
    assert wire["severity"] == "critical"
    assert wire["title"] == "SPOTTER AI has detected an issue"
    assert wire["body"] == "run-012 anomalous (mean_biomass z=6.1). Campaign quarantined."
    # status reuses the existing shared field-group slot; MVP default is "active".
    assert wire["status"] == "active"
    assert wire["actions"] == _CONTRACT_ACTIONS
    # omit-empty: fields from OTHER part kinds never leak onto an action_card.
    for stray in ("text", "path", "unified_diff", "call_id", "tool_name", "summary"):
        assert stray not in wire


def test_action_card_part_omits_empty_actions() -> None:
    part = action_card_part(source="spotter-ai", severity="info", title="t", body="b")
    wire = part.to_wire()
    assert "actions" not in wire


def test_action_card_part_status_is_overridable() -> None:
    part = action_card_part(
        source="spotter-ai", severity="info", title="t", body="b", status="resolved"
    )
    assert part.to_wire()["status"] == "resolved"


# --------------------------------------------------------------------------- #
# 2. emit is turn-agnostic and visible through the real GET /messages surface
# --------------------------------------------------------------------------- #


def test_emit_with_no_active_turn_creates_visible_message_part(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        part = action_card_part(
            source="spotter-ai",
            severity="warning",
            title="Heads up",
            body="something needs a look",
            actions=[{"id": "discuss", "label": "Discuss", "enabled": True, "behavior": {}}],
        )
        emit_action_card_part(app, sid, part)

        # Consumer's semantics: read via the real API surface, not the internal dict.
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        cards = [p for m in messages for p in m["parts"] if p["type"] == "action_card"]
        assert len(cards) == 1
        assert cards[0]["title"] == "Heads up"
        assert cards[0]["severity"] == "warning"
        assert cards[0]["status"] == "active"


# --------------------------------------------------------------------------- #
# 3. raise_alert_card native tool: child -> parent, and the no-parent error
# --------------------------------------------------------------------------- #


def _register_fake_child_task(
    app, *, parent_sid: str, expert_id: str = "spotter_watcher", blueprint_id: str = ""
) -> AgentTask:
    """Mint a real child session + register a real AgentTask, without driving a
    turn (this test targets ``raise_alert_card``'s own logic, not the spawn
    substrate — that is covered end-to-end in test_spotter_watcher.py).

    ``blueprint_id``, when given, mirrors ``turn_spawn.spawn_child_turn``'s
    real behavior: it stamps the child's OWN ``active_agent_blueprint_id``
    metadata (via ``session_scope_metadata``), the exact field
    ``raise_alert_card`` reads for the card's branded ``source``.
    """

    child = app.state.sessions.create(
        workspace_id="ws_default",
        parent_session_id=parent_sid,
        agent={"id": expert_id, "mode": "subagent"},
    )
    if blueprint_id:
        app.state.sessions.update(
            child.id, metadata_patch={"active_agent_blueprint_id": blueprint_id}
        )
    task = AgentTask(
        task_id="task_" + child.id.split("_")[-1],
        parent_session_id=parent_sid,
        child_session_id=child.id,
        agent_ref={"expert_id": expert_id, "requesting_expert_id": "main"},
        status=STATUS_RUNNING,
        live_state=STATUS_RUNNING,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    app.state.agent_task_registry.register(task)
    return task


def _call_tool_as(app, session_id: str, tool, **kwargs):
    """Invoke a native tool's underlying callable with the runtime context a
    real react step would carry (active app + active session)."""

    app_token = _ctx.set_app(app)
    sid_token = _ctx.set_session_id(session_id)
    try:
        return tool.func(**kwargs)
    finally:
        _ctx.reset(sid_token)
        _ctx.reset(app_token)


def test_raise_alert_card_from_child_emits_into_parent_with_discuss_handle(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent_sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _register_fake_child_task(app, parent_sid=parent_sid, blueprint_id="spotter-ai")

        tool = build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="Spotter Watcher"))
        result = _call_tool_as(
            app,
            task.child_session_id,
            tool,
            title="Anomalous run",
            body="run-012 anomalous (mean_biomass z=6.1).",
            severity="critical",
            stub_actions=[{"id": "address", "label": "Address", "reason": "phase 2"}],
        )

        assert result == {
            "emitted": True,
            "session_id": parent_sid,
            "part_id": result["part_id"],
        }

        messages = client.get(f"/v1/sessions/{parent_sid}/messages").json()["messages"]
        cards = [p for m in messages for p in m["parts"] if p["type"] == "action_card"]
        assert len(cards) == 1
        card = cards[0]
        # source is the calling session's BRANDED blueprint identity ("spotter-ai"),
        # never the bare expert id -- the card header must read as the product, not
        # an internal expert name. agent_id stays the expert-attribution field.
        assert card["source"] == "spotter-ai"
        assert card["agent_id"] == "spotter_watcher"
        assert card["severity"] == "critical"
        assert card["title"] == "Anomalous run"
        actions = card["actions"]
        assert actions[0] == {
            "id": "discuss",
            "label": "Discuss",
            "enabled": True,
            "behavior": {"kind": "focus_session", "handle_id": task.task_id},
        }
        assert actions[1] == {
            "id": "address",
            "label": "Address",
            "enabled": False,
            "behavior": {"kind": "stub", "reason": "phase 2"},
        }

        # Nothing was emitted into the CHILD's own transcript.
        child_messages = client.get(
            f"/v1/sessions/{task.child_session_id}/messages"
        ).json()["messages"]
        assert not any(p["type"] == "action_card" for m in child_messages for p in m["parts"])


def test_raise_alert_card_source_falls_back_to_expert_id_without_blueprint(
    tmp_path: Path,
) -> None:
    """A calling session with no activated blueprint (a bare/loose expert) falls
    back to the expert id for ``source`` -- never an empty branded identity."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent_sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _register_fake_child_task(app, parent_sid=parent_sid)  # no blueprint_id

        tool = build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="Spotter Watcher"))
        _call_tool_as(app, task.child_session_id, tool, title="t", body="b")

        messages = client.get(f"/v1/sessions/{parent_sid}/messages").json()["messages"]
        cards = [p for m in messages for p in m["parts"] if p["type"] == "action_card"]
        assert cards[0]["source"] == "spotter_watcher"


def test_raise_alert_card_accepts_bare_string_stub_actions(tmp_path: Path) -> None:
    """A model passing bare strings (``["address", "remove"]``) must not crash
    the tool -- coerced into disabled stub buttons with an honest fixed reason."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent_sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _register_fake_child_task(app, parent_sid=parent_sid)

        tool = build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="Spotter Watcher"))
        result = _call_tool_as(
            app,
            task.child_session_id,
            tool,
            title="t",
            body="b",
            stub_actions=["address", "remove"],
        )
        assert result["emitted"] is True

        messages = client.get(f"/v1/sessions/{parent_sid}/messages").json()["messages"]
        cards = [p for m in messages for p in m["parts"] if p["type"] == "action_card"]
        actions = cards[0]["actions"]
        assert actions[1] == {
            "id": "address",
            "label": "Address",
            "enabled": False,
            "behavior": {"kind": "stub", "reason": "not yet implemented"},
        }
        assert actions[2] == {
            "id": "remove",
            "label": "Remove",
            "enabled": False,
            "behavior": {"kind": "stub", "reason": "not yet implemented"},
        }


def test_raise_alert_card_reports_skipped_unsupported_stub_actions(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent_sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _register_fake_child_task(app, parent_sid=parent_sid)

        tool = build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="Spotter Watcher"))
        result = _call_tool_as(
            app, task.child_session_id, tool, title="t", body="b", stub_actions=[123, None]
        )

        assert result["emitted"] is True
        assert result["skipped_stub_actions"] == [
            {"value": "123", "reason": "unsupported_stub_action_type"},
            {"value": "None", "reason": "unsupported_stub_action_type"},
        ]


def test_raise_alert_card_without_parent_returns_typed_error(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "bare"}).json()["id"]

        tool = build_raise_alert_card_tool(AgentDef(id="main", title="Main"))
        result = _call_tool_as(app, sid, tool, title="x", body="y")

        assert result["error"] == ALERT_CARD_NO_PARENT_ERROR
        # Never emitted anywhere, silently or otherwise.
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert not any(p["type"] == "action_card" for m in messages for p in m["parts"])


def test_raise_alert_card_with_parent_but_no_task_row_returns_distinct_typed_error(
    tmp_path: Path,
) -> None:
    """A session that DOES have a parent_session_id (it was spawned) but whose
    AgentTask row is missing (a race, or a pre-projection session) is a
    DIFFERENT reality from having no parent at all -- distinct typed reason."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent_sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        child = app.state.sessions.create(workspace_id="ws_default", parent_session_id=parent_sid)
        # Deliberately no AgentTask registered for `child`.

        tool = build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="Spotter Watcher"))
        result = _call_tool_as(app, child.id, tool, title="x", body="y")

        assert result["error"] == ALERT_CARD_TASK_ROW_MISSING_ERROR


# --------------------------------------------------------------------------- #
# 3b. emit_action_card_part against a REAL live-turn ledger: lands when open,
#     reports the typed drop when frozen (never a silent swallow).
# --------------------------------------------------------------------------- #


def _open_turn_ledger(app: Any, sid: str, turn_id: str = "turn_live") -> Any:
    from clio_agent.gact.transcript import EventBusTranscriptPublisher

    return app.state.turn_transcripts.open_turn(
        sid, turn_id, EventBusTranscriptPublisher(app.state.bus, sid)
    )


def test_emit_while_parent_turn_is_live_lands_through_the_real_ledger(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _open_turn_ledger(app, sid)

        part = action_card_part(source="spotter-ai", severity="info", title="Live", body="b")
        assert emit_action_card_part(app, sid, part) is True

        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        cards = [p for m in messages for p in m["parts"] if p["type"] == "action_card"]
        assert len(cards) == 1
        assert cards[0]["title"] == "Live"


def test_emit_against_a_frozen_ledger_reports_typed_drop_not_silent(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        transcript = _open_turn_ledger(app, sid)
        transcript.abandon()  # freeze without publishing -- the settle-race shape

        part = action_card_part(source="spotter-ai", severity="info", title="Dropped", body="b")
        assert emit_action_card_part(app, sid, part) is False

        # Never silently landed anywhere.
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert not any(p["type"] == "action_card" for m in messages for p in m["parts"])


def test_raise_alert_card_against_a_frozen_parent_ledger_reports_typed_drop(
    tmp_path: Path,
) -> None:
    """End to end through the tool: a frozen PARENT ledger never lies with an
    unconditional ``emitted: true`` (finding: this used to be unconditional)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent_sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _register_fake_child_task(app, parent_sid=parent_sid)
        _open_turn_ledger(app, parent_sid).abandon()

        tool = build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="Spotter Watcher"))
        result = _call_tool_as(app, task.child_session_id, tool, title="t", body="b")

        assert result == {
            "emitted": False,
            "reason": ALERT_CARD_PARENT_TRANSCRIPT_FROZEN,
            "session_id": parent_sid,
        }


# --------------------------------------------------------------------------- #
# 4. raise_alert_card is auto-attached on BOTH real module-assembly paths,
#    even for an expert that declares its OWN curated ``tools:`` list.
# --------------------------------------------------------------------------- #


def _patch_lm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in LM wiring (mirrors test_toolset_inventory.py): these tests only
    exercise the BUILD path, never forward()."""

    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: SimpleNamespace(
            materialize=lambda cred=None: SimpleNamespace(
                provider="openai", model="m", temperature=0.0
            )
        ),
        raising=True,
    )


def _build_under_turn_identity(
    build_fn: Callable[[Any, AgentDef], Any],
    base_agent: Any,
    agent_def: AgentDef,
    app: Any,
    sid: str,
) -> Any:
    """Build a react module inside a full turn identity (mirrors
    test_toolset_inventory.py's idiom): ``set_turn_identity`` is a bare set (no
    token), so run it inside a copied context so the identity never leaks past
    this call."""

    def _run() -> Any:
        _ctx.set_turn_identity(app=app, session_id=sid, turn_id="turn_t", trace_id="trace_t")
        return build_fn(base_agent, agent_def)

    return contextvars.copy_context().run(_run)


def _tool_names(module: Any) -> set[str]:
    return {str(getattr(t, "name", "")) for t in module.tools}


def _fs_tool_executor() -> Any:
    """A base_agent tool_executor that resolves ``fs_read_file`` (the curated tool
    these fixtures declare), mirroring test_toolset_inventory.py's shape."""

    return SimpleNamespace(
        to_dspy_tools=lambda: [SimpleNamespace(name="fs_read_file")],
        _namespace_servers={"fs": object()},
    )


def test_raise_alert_card_auto_attached_for_blueprint_react_expert_with_curated_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tier-1 (react-main-shaped) assembly path: a blueprint expert that
    declares its OWN curated MCP tool list must STILL receive raise_alert_card —
    it rides the SEPARATE auto-attached merge (auto_tools.py), not the curated
    ``_dynamic_agent_tools`` result."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _patch_lm(monkeypatch)

        base_agent = SimpleNamespace(tool_executor=_fs_tool_executor())
        agent_def = AgentDef(
            id="curated_react_expert",
            source="expert_pack",
            title="Curated",
            system_prompt="Do curated work.",
            module={"kind": "react"},
            tools=["fs_read_file"],
        )

        module = _build_under_turn_identity(
            _build_blueprint_dspy_module, base_agent, agent_def, app, sid
        )

        names = _tool_names(module)
        assert "fs_read_file" in names, names  # the curated declaration still resolves
        assert "raise_alert_card" in names, names  # auto-attached regardless


def test_raise_alert_card_auto_attached_for_tool_user_agent_with_curated_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SECOND assembly path (``_build_tool_user_agent_module``, the one the
    coordinator flagged): same guarantee for a tool-declaring dynamic agent."""

    from clio_agent.gact.agents.builders import _build_tool_user_agent_module

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _patch_lm(monkeypatch)

        base_agent = SimpleNamespace(tool_executor=_fs_tool_executor())
        agent_def = AgentDef(
            id="curated_tool_user_agent",
            source="user",
            title="Curated Tool User",
            system_prompt="Do curated work.",
            module={},
            tools=["fs_read_file"],
        )

        module = _build_under_turn_identity(
            _build_tool_user_agent_module, base_agent, agent_def, app, sid
        )

        names = _tool_names(module)
        assert "fs_read_file" in names, names
        assert "raise_alert_card" in names, names
