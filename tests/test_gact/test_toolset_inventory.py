"""``agent.toolset.recorded`` -- the server half of the obs Tools tab's
"called | available" toggle.

Fires once per built react expert, at the ``instrument_tools()`` assembly
seam in ``builders.py`` (the blueprint react branch AND the tool-user-agent
branch), carrying the REAL final toolset -- never a recomputed guess. It
rides the existing semantic-trace highway (``_emit_semantic_event``), so the
obs UI reads it back from the SAME ``GET /v1/sessions/{sid}/trace`` route it
already polls for parent + children. No new store, no new route (RULE 4).
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef


def _patch_lm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in LM wiring: these tests only exercise the BUILD path, never forward()."""

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


def _capture_emit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every ``_emit_semantic_event`` call the SAME way ``test_skill_events.py``
    does: the builders module does a lazy ``from ...globals import _emit_semantic_event``
    per call, so patching the module attribute is picked up on the next build."""

    captured: list[dict[str, Any]] = []

    def _capture(app: Any, sid: str, event_type: str, **kw: Any) -> dict[str, Any]:
        captured.append({"event_type": event_type, "session_id": sid, **kw})
        return {}

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", _capture)
    return captured


def _events_of(captured: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event_type") == event_type]


def _build_under_turn_identity(
    build_fn: Callable[[Any, AgentDef], Any],
    base_agent: Any,
    agent_def: AgentDef,
    app: Any,
    sid: str,
) -> Any:
    """Build a react module inside a full turn identity (app/session/turn/trace).

    ``set_turn_identity`` is a BARE set (no token) -- run inside a copied context
    (the ``test_skill_events.py`` idiom) so the identity never leaks past this call.
    """

    def _run() -> Any:
        _ctx.set_turn_identity(app=app, session_id=sid, turn_id="turn_tsi", trace_id="trace_tsi")
        return build_fn(base_agent, agent_def)

    return contextvars.copy_context().run(_run)


@pytest.fixture
def live_app(tmp_path: Path) -> Any:
    application = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(application) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        yield application, sid


def test_blueprint_react_build_emits_toolset_recorded_with_native_tools(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain react root (no tools/skills/children declared) still built the
    auto-attached native tool set -- one inventory event, shaped
    {agent_id, session_id, tools:[{name,title,source,representation}]}, matching
    the module's REAL ``.tools`` 1:1 (never a subset/superset)."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    application, sid = live_app
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="toolset_inventory_leaf_9f2c",
        source="expert_pack",
        title="Leaf",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    module = _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    events = _events_of(captured, "agent.toolset.recorded")
    assert len(events) == 1
    event = events[0]
    assert event["turn_id"] == "turn_tsi"
    assert event["trace_id"] == "trace_tsi"
    assert event["session_id"] == sid
    payload = event["payload"]
    assert payload["agent_id"] == "toolset_inventory_leaf_9f2c"
    assert payload["session_id"] == sid

    rows = {row["name"]: row for row in payload["tools"]}
    real_names = {str(getattr(t, "name", "")) for t in module.tools}
    assert set(rows) == real_names
    for row in rows.values():
        assert set(row) == {"name", "title", "source", "representation"}

    # Auto-attached infra is native, never mistaken for an MCP server or the
    # spawn-runtime surface.
    assert rows["create_artifact"]["source"] == "native"
    assert rows["create_artifact"]["representation"] == "chip"
    assert rows["write_todos"]["source"] == "native"
    assert rows["goal_status"]["source"] == "native"
    # No declared children on this def -> the spawn-routing surface is absent.
    assert "spawn_agent_task" not in rows


def test_tool_user_agent_build_also_emits_toolset_recorded(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second call site (``_build_tool_user_agent_module``) fires the same event."""

    from clio_agent.gact.agents.builders import _build_tool_user_agent_module

    application, sid = live_app
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="analyst",
        source="user",
        title="Analyst",
        system_prompt="Analyze.",
        module={},
    )

    module = _build_under_turn_identity(
        _build_tool_user_agent_module, base_agent, agent_def, application, sid
    )

    events = _events_of(captured, "agent.toolset.recorded")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["agent_id"] == "analyst"
    assert {row["name"] for row in payload["tools"]} == {
        str(getattr(t, "name", "")) for t in module.tools
    }


def test_gateway_and_external_mcp_tools_carry_their_real_source(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``source`` is the ACTUAL provenance: a gateway-mounted tool's namespace
    prefix (the same encoding ``mcp_executor._route`` dispatches on) for the
    base fleet, and the declared external MCP server id for an Agent Blueprint
    MCP tool -- never a generic 'mcp' bucket that would erase which server."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    application, sid = live_app
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)

    application.state.external_mcp_servers = {
        "agent_blueprint_mcp_earth_earthscope": {
            "id": "agent_blueprint_mcp_earth_earthscope",
            "status": "ready",
            "tools": [
                {
                    "id": "earthscope_query",
                    "name": "earthscope_query",
                    "description": "query EarthScope catalog",
                    "status": "ready",
                    "enabled": True,
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
    }
    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(
            to_dspy_tools=lambda: [SimpleNamespace(name="fs_read_file")]
        )
    )
    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        system_prompt="Root.",
        module={"kind": "react"},
        tools=["fs_read_file", "earthscope_query"],
    )

    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    rows = {
        row["name"]: row
        for row in _events_of(captured, "agent.toolset.recorded")[0]["payload"]["tools"]
    }
    assert rows["fs_read_file"]["source"] == "fs"
    assert rows["earthscope_query"]["source"] == "agent_blueprint_mcp_earth_earthscope"


def test_declared_children_give_spawn_tools_the_spawn_runtime_source(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A react main with declared children gets the spawn/wait/check surface,
    and every one of those tools is labeled ``source: "spawn-runtime"`` --
    never confused with a curated domain tool."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    application, sid = live_app
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": {"child_a"},
    )

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    rows = {
        row["name"]: row
        for row in _events_of(captured, "agent.toolset.recorded")[0]["payload"]["tools"]
    }
    assert rows["spawn_agent_task"]["source"] == "spawn-runtime"
    assert rows["wait_agent_tasks"]["source"] == "spawn-runtime"
    assert rows["check_agent_tasks"]["source"] == "spawn-runtime"
    assert rows["spawn_agents_parallel"]["source"] == "spawn-runtime"


def test_no_app_or_session_skips_event_with_a_structured_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorder handle being unreachable is a FINDING (logged), never a
    silent pass: a build with no active app/session context leaves no event
    but DOES leave a traceable reason (no-silent-fallback ground rule)."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    reasons: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders.trace.event",
        lambda tag, msg, *a: reasons.append(msg % a if a else msg),
    )
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    # No _ctx.set_turn_identity / set_app here: the ambient context carries no
    # active app or session, mirroring a build reached off the normal turn path.
    _build_blueprint_dspy_module(base_agent, agent_def)

    assert _events_of(captured, "agent.toolset.recorded") == []
    assert any("skipped reason=no_app_or_session" in r for r in reasons)


def test_emit_failure_never_breaks_a_successful_build(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observability must not destroy the observed work: an ARC/sink failure in
    the emitter leaves the build intact and a structured reason in the trace."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    application, sid = live_app
    _patch_lm(monkeypatch)

    reasons: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders.trace.event",
        lambda tag, msg, *a: reasons.append(msg % a if a else msg),
    )

    def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("ARC unavailable")

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", _boom)

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    module = _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    assert module.tools  # the build itself succeeded despite the emit failure
    assert any("agent.toolset.recorded emit failed" in r for r in reasons)
