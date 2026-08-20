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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    MCP tool -- never a generic 'mcp' bucket that would erase which server.

    Finding [D]: the namespace prefix is trusted ONLY when it names a namespace
    the tool_executor ACTUALLY mounts (``_namespace_servers``, read the same set
    ``mcp_executor._route`` validates against) -- ``fs`` is mounted so
    ``fs_read_file`` labels itself, but a name whose prefix is NOT in that set
    (``ghost_probe``) and a name with no underscore at all (``solo``) both fall
    back to the literal "gateway" bucket instead of a fabricated namespace."""

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
            to_dspy_tools=lambda: [
                SimpleNamespace(name="fs_read_file"),
                SimpleNamespace(name="ghost_probe_scan"),
                SimpleNamespace(name="solo"),
            ],
            # The real dispatcher's mounted-namespace set (finding [D]): only "fs"
            # is actually mounted, so "ghost_probe_scan" is an unmounted prefix.
            _namespace_servers={"fs": object()},
        )
    )
    agent_def = AgentDef(
        id="main",
        source="expert_pack",
        title="Main",
        system_prompt="Root.",
        module={"kind": "react"},
        tools=["fs_read_file", "ghost_probe_scan", "solo", "earthscope_query"],
    )

    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    rows = {
        row["name"]: row
        for row in _events_of(captured, "agent.toolset.recorded")[0]["payload"]["tools"]
    }
    assert rows["fs_read_file"]["source"] == "fs"
    assert rows["ghost_probe_scan"]["source"] == "gateway"
    assert rows["solo"]["source"] == "gateway"
    assert rows["earthscope_query"]["source"] == "agent_blueprint_mcp_earth_earthscope"


def test_mounted_namespace_set_reads_the_executors_namespace_servers() -> None:
    """Finding [D] unit coverage: the accessor reads ``_namespace_servers`` off
    either a direct executor or one wrapped in ``_async_executor`` (the real
    ``SyncMCPToolExecutor`` shape), and returns empty for neither shape."""

    from clio_agent.gact.agents import toolset_inventory

    direct = SimpleNamespace(_namespace_servers={"fs": object(), "shell": object()})
    assert toolset_inventory.mounted_namespace_set(direct) == {"fs", "shell"}

    wrapped = SimpleNamespace(
        _async_executor=SimpleNamespace(_namespace_servers={"relay": object()})
    )
    assert toolset_inventory.mounted_namespace_set(wrapped) == {"relay"}

    assert toolset_inventory.mounted_namespace_set(SimpleNamespace()) == set()


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


# ---------------------------------------------------------------------------
# Finding [B]: _TOOL_SOURCES was a module-level dict keyed by tool NAME only.
# Child turns build concurrently on a ThreadPoolExecutor and external-MCP tool
# names are not namespaced at registration, so two concurrent builds with a
# colliding tool name could race and hand one build the OTHER build's source.
# The fix deletes the shared registry entirely -- every construction site
# builds its OWN per-call ``sources`` dict, threaded explicitly into
# ``emit_agent_toolset_recorded``.
# ---------------------------------------------------------------------------


def test_no_shared_module_global_survives_the_fix() -> None:
    """The module-level ``_TOOL_SOURCES`` registry is DELETED, not merely
    reset between tests -- there is no shared mutable state left to race on."""

    from clio_agent.gact.agents import toolset_inventory

    assert not hasattr(toolset_inventory, "_TOOL_SOURCES")


def test_concurrent_registrations_with_a_colliding_name_never_cross_contaminate() -> None:
    """Failing-first for finding [B]: two builds race on a ThreadPoolExecutor and
    BOTH register a tool literally named "shared_tool" -- one sourced from
    server_a, the other from server_b. Under the deleted module-global design
    both writes landed on the SAME shared dict key, so whichever build wrote
    last silently overwrote the other's fact and a subsequent read (widened
    here by an explicit sleep between register and read) could return the
    WRONG build's source. Per-call dicts make that impossible: each build's
    ``declared_tool_source`` reads back exactly what THAT build registered."""

    from clio_agent.gact.agents import toolset_inventory

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def _one_build(server_id: str) -> None:
        sources: dict[str, str] = {}
        barrier.wait()  # maximize interleaving between the two builds
        toolset_inventory.register_tool_source(sources, "shared_tool", server_id)
        time.sleep(0.02)  # widen the race window a stale shared dict would expose
        results[server_id] = toolset_inventory.declared_tool_source(sources, "shared_tool")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_one_build, "server_a"), pool.submit(_one_build, "server_b")]
        for future in futures:
            future.result()

    assert results == {"server_a": "server_a", "server_b": "server_b"}


# ---------------------------------------------------------------------------
# Finding [C]: turn_forward rebuilds a fresh module every turn, and the
# stream_fallback compat path rebuilds AGAIN in the same turn -- an N-turn
# session was accumulating N x experts identical events. The fix compares the
# built toolset against the last one recorded for (session_id, agent_id) and
# emits only on a genuine change.
# ---------------------------------------------------------------------------


def test_identical_rebuild_in_the_same_session_emits_only_once(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    application, sid = live_app
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="dup_agent",
        source="expert_pack",
        title="Dup",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )
    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    assert len(_events_of(captured, "agent.toolset.recorded")) == 1


def test_changed_toolset_in_the_same_session_emits_again(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedup is CONTENT-based, not a blanket once-per-agent suppression: a
    genuinely different toolset (declared children appear between builds, so
    the spawn-runtime tools join the set) re-emits."""

    from clio_agent.gact.agents.builders import _build_blueprint_dspy_module

    application, sid = live_app
    _patch_lm(monkeypatch)
    captured = _capture_emit(monkeypatch)

    base_agent = SimpleNamespace(tool_executor=None)
    agent_def = AgentDef(
        id="grow_agent",
        source="expert_pack",
        title="Grow",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": {"child_a"},
    )
    _build_under_turn_identity(
        _build_blueprint_dspy_module, base_agent, agent_def, application, sid
    )

    events = _events_of(captured, "agent.toolset.recorded")
    assert len(events) == 2
    assert "spawn_agent_task" not in {row["name"] for row in events[0]["payload"]["tools"]}
    assert "spawn_agent_task" in {row["name"] for row in events[1]["payload"]["tools"]}


# ---------------------------------------------------------------------------
# Finding [F]: the skip/emit-failure reasons were reachable ONLY through the
# CLIO_DEBUG-gated logger. The fix additionally records both branches into a
# structured, per-session app.state catalog (patterned on streaming.py's
# _stream_fallback_reasons) -- queryable after the fact, not just a log line.
# ---------------------------------------------------------------------------


def test_skip_reason_reaches_the_structured_catalog_when_app_is_reachable(
    live_app: Any,
) -> None:
    """The app-reachable half of the skip branch (app bound, but no session id
    bound) records into ``app.state``'s catalog. The OTHER half (no app at
    all, covered by ``test_no_app_or_session_skips_event_with_a_structured_reason``
    above) has no ``app.state`` to record into -- that is the one branch the
    catalog genuinely cannot reach; the CLIO_DEBUG log stays its only trace."""

    from clio_agent.gact.agents import toolset_inventory

    application, _sid = live_app
    agent_def = AgentDef(
        id="orphan",
        source="expert_pack",
        title="Orphan",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    def _run() -> None:
        _ctx.set_app(application)  # app bound; NO session id bound
        toolset_inventory.emit_agent_toolset_recorded(agent_def, [], {})

    contextvars.copy_context().run(_run)

    reasons = toolset_inventory.toolset_inventory_reasons(application, "")
    assert len(reasons) == 1
    assert reasons[0]["reason"] == "no_session"
    assert reasons[0]["agent_id"] == "orphan"


def test_emit_failure_reason_reaches_the_structured_catalog(
    live_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents import toolset_inventory

    application, sid = live_app

    def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("ARC unavailable")

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", _boom)
    agent_def = AgentDef(
        id="flaky",
        source="expert_pack",
        title="Flaky",
        system_prompt="Root.",
        module={"kind": "react"},
    )

    def _run() -> None:
        _ctx.set_turn_identity(app=application, session_id=sid, turn_id="t", trace_id="tr")
        toolset_inventory.emit_agent_toolset_recorded(agent_def, [], {})

    contextvars.copy_context().run(_run)

    reasons = toolset_inventory.toolset_inventory_reasons(application, sid)
    assert len(reasons) == 1
    assert reasons[0]["reason"] == "emit_failed"
    assert reasons[0]["agent_id"] == "flaky"
    assert "ARC unavailable" in reasons[0]["detail"]
