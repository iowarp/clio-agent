"""skill.loaded provenance events + evidence skill_resolution (#916 S4 / #920)."""

from __future__ import annotations

import contextvars
import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import skill_runtime as sr
from clio_agent.gact.app import build_app
from clio_agent.gact.semantic_events import event_reaches_ui
from clio_agent.gact.types import AgentDef

BODY = "PROCEDURE BODY."


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    d = tmp_path / "pack" / "skills" / "rubric"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: rubric\ndescription: R\n---\n\n{BODY}\n", encoding="utf-8"
    )
    (d / "extra.md").write_text("EXTRA", encoding="utf-8")
    return tmp_path / "pack"


def _agent(pack: Path) -> AgentDef:
    return AgentDef(
        id="analyst",
        source="expert_pack",
        title="Analyst",
        skills=["rubric"],
        metadata={"pack_definition_path": str(pack / "AGENT.md")},
    )


def _events_of(captured: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event_type") == event_type]


def test_load_skill_emits_exactly_one_skill_loaded(
    pack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One load → one skill.loaded with full provenance (id/scope/path/checksum/
    size/agent); a bundled-file load carries the file name."""

    captured: list[dict[str, Any]] = []

    def _capture(app: Any, sid: str, event_type: str, **kw: Any) -> dict[str, Any]:
        captured.append({"event_type": event_type, **kw})
        return {}

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", _capture)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        agent = _agent(pack)
        runtime = sr.skill_runtime_for_agent(app, agent)
        tool = sr.build_load_skill_tool(agent, runtime)

        def _call_under_turn() -> None:
            # set_turn_identity is a BARE set (no token) — run inside a copied
            # context so the identity never leaks past this test.
            _ctx.set_turn_identity(
                app=app, session_id=sid, turn_id="turn_920", trace_id="trace_920"
            )
            tool.func(skill_id="rubric")
            tool.func(skill_id="rubric", file="extra.md")

        contextvars.copy_context().run(_call_under_turn)

    loaded = _events_of(captured, "skill.loaded")
    assert len(loaded) == 2
    # Correlated to the turn like every in-loop emitter — never orphaned.
    assert loaded[0]["turn_id"] == "turn_920"
    assert loaded[0]["trace_id"] == "trace_920"
    body_event = loaded[0]["payload"]
    assert body_event["skill_id"] == "rubric"
    assert body_event["scope"] == "pack"
    assert body_event["agent_id"] == "analyst"
    assert Path(body_event["path"]).name == "SKILL.md"
    raw = Path(body_event["path"]).read_bytes()
    assert body_event["checksum"] == hashlib.sha256(raw).hexdigest()
    assert body_event["size"] > 0
    assert loaded[1]["payload"]["file"] == "extra.md"


def test_skill_loaded_reaches_the_ui_wire() -> None:
    assert event_reaches_ui("skill.loaded")


def test_load_outside_session_is_traced_not_lost(
    pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No app/session context: the load still works and leaves a structured
    trace reason instead of a silent no-event."""

    reasons: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.skill_runtime.trace.event",
        lambda tag, msg, *args: reasons.append(msg % args if args else msg),
    )
    agent = _agent(pack)
    runtime = sr.skill_runtime_for_agent(None, agent)
    out = sr.build_load_skill_tool(agent, runtime).func(skill_id="rubric")
    assert BODY in out
    assert any("outside app/session" in r for r in reasons)


def test_emit_failure_never_breaks_a_successful_load(
    pack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observability must not destroy the observed work: an ARC/sink failure in
    the emitter leaves the load intact and a structured reason in the trace."""

    reasons: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.skill_runtime.trace.event",
        lambda tag, msg, *args: reasons.append(msg % args if args else msg),
    )

    def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("ARC unavailable")

    monkeypatch.setattr("clio_agent.gact.runtime.globals._emit_semantic_event", _boom)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        agent = _agent(pack)
        runtime = sr.skill_runtime_for_agent(app, agent)
        tool = sr.build_load_skill_tool(agent, runtime)

        def _call_under_turn() -> str:
            _ctx.set_turn_identity(
                app=app, session_id=sid, turn_id="turn_920", trace_id="trace_920"
            )
            return tool.func(skill_id="rubric")

        out = contextvars.copy_context().run(_call_under_turn)
    assert BODY in out
    assert any("emit failed" in r for r in reasons)


def test_runtime_provenance_carries_resolved_skills_from_runtime_truth(
    pack: Path, tmp_path: Path
) -> None:
    """resolved_skills reflects what the EXECUTING expert actually had (the
    build cache), not just the row-load snapshot — source-agnostic."""

    from clio_agent.gact.evidence import _dynamic_agent_runtime_provenance

    app = build_app(sessions_path=tmp_path / "s.json")
    agent = _agent(pack)
    runtime = sr.skill_runtime_for_agent(app, agent)  # fills the per-app cache
    assert runtime.resolved
    payload = _dynamic_agent_runtime_provenance(app, agent, execution_mode="blueprint")
    assert payload["resolved_skills"]["rubric"]["status"] == "resolved"
    assert payload["resolved_skills"]["rubric"]["scope"] == "pack"


def test_runtime_provenance_carries_skill_resolution(pack: Path, tmp_path: Path) -> None:
    """The dynamic-agent runtime provenance includes the typed per-id resolution."""

    from clio_agent.gact.evidence import _dynamic_agent_runtime_provenance

    app = build_app(sessions_path=tmp_path / "s.json")
    agent = _agent(pack).model_copy(
        update={
            "metadata": {
                "pack_definition_path": str(pack / "AGENT.md"),
                "skill_resolution": {
                    "rubric": {"id": "rubric", "status": "resolved", "scope": "pack"},
                    "ghost": {"id": "ghost", "status": "missing"},
                },
            }
        }
    )
    payload = _dynamic_agent_runtime_provenance(app, agent, execution_mode="blueprint")
    assert payload["skill_resolution"]["rubric"]["status"] == "resolved"
    assert payload["skill_resolution"]["ghost"]["status"] == "missing"


def test_load_skill_call_is_recorded_on_the_blueprint_tool_rows(pack: Path) -> None:
    """The auto-attached load_skill is wrapped like a declared tool: calling it
    lands a row on the active blueprint tool rows — the stream that becomes
    tool_call/tool_result transcript parts — so a skill load is visible in the
    loop, not only in the trace log. (Found live: the materio-md compute expert
    loaded both its skills with zero wire evidence.)"""

    from clio_agent.gact.agents.builders import _recorded_load_skill_tool

    agent = _agent(pack)
    runtime = sr.skill_runtime_for_agent(None, agent)
    tool = _recorded_load_skill_tool(agent, runtime)
    assert getattr(tool, "name", "") == "load_skill"

    rows: list[dict[str, Any]] = []

    def _call_with_rows() -> str:
        _ctx.set_blueprint_tool_rows(rows)
        return tool.func(skill_id="rubric")

    out = contextvars.copy_context().run(_call_with_rows)
    assert BODY in out
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "load_skill"
    assert row["ok"] is True
    assert row["args"] == {"skill_id": "rubric"}
    assert row["telemetry_source"] == "blueprint_react_tool_wrapper"


def test_failed_load_skill_call_is_recorded_as_an_error_row(pack: Path) -> None:
    """An unknown-skill load still leaves wire evidence: the recording wrapper
    appends an ok=False row with the error before re-raising."""

    from clio_agent.gact.agents.builders import _recorded_load_skill_tool

    agent = _agent(pack)
    runtime = sr.skill_runtime_for_agent(None, agent)
    tool = _recorded_load_skill_tool(agent, runtime)

    rows: list[dict[str, Any]] = []

    def _call_with_rows() -> None:
        _ctx.set_blueprint_tool_rows(rows)
        with pytest.raises(ValueError, match="unknown skill"):
            tool.func(skill_id="nope")

    contextvars.copy_context().run(_call_with_rows)
    assert len(rows) == 1
    assert rows[0]["name"] == "load_skill"
    assert rows[0]["ok"] is False
    assert "unknown skill" in rows[0]["error"]
