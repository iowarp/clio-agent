"""Parity tests for the one agent-resolution seam (#770 Wave-C C1).

The routes (``GET /v1/agents``, ``GET /v1/sessions/{sid}/agent``) and the turn
engine (``clio_agent.gact.agents.resolution``) must describe the SAME agent for a
session: the session's pinned plugin resolution. Before the C1 unification the
route and the runtime were two independent stacks that disagreed; these tests
lock the single-seam invariant so the divergence cannot come back.

Every session runs a resolved plugin agent -- ``clio/standard`` from the
hermetic test marketplace by default -- and a session never silently inherits
an agent it did not select: an unresolvable agent fails typed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents.resolution import (
    apply_session_tool_allowlist,
    resolution_agent_def,
    resolve_session_agent,
    session_agent_def,
    session_resolution,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.types import AgentDef
from tests._plugin_fixtures import custom_marketplace

#: A plugin that ships one defined subagent, carried by the agent ``reviewer``.
_REVIEW_PLUGIN = {
    "files": {
        "ai.iowarp.clio/subagents/critic.py": (
            "from clio_agent.sdk import Subagent\n\n"
            "critic = Subagent('critic', prompt='Critique the draft.', "
            "plugins=['workspace'], description='Critiques drafts.')\n"
        ),
    }
}

_REVIEWER_AGENT = """
from clio_agent.sdk import Agent


class Reviewer(Agent):
    id = "reviewer"
    version = "1.0.0"
    name = "Reviewer"
    role = "a document reviewer"
    plugins = Agent.plugins + ["workspace", "review"]
"""


def _session(client: TestClient, **metadata: Any) -> str:
    resp = client.post("/v1/sessions", json={"title": "parity", "metadata": metadata})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_route_and_runtime_describe_the_same_session_agent(tmp_path: Path) -> None:
    """#1: the session-agent route and the runtime agent agree on one resolution."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = _session(client)
        route = client.get(f"/v1/sessions/{sid}/agent").json()["resolution"]
        listed = {row["ref"] for row in client.get("/v1/agents").json()["agents"]}
        runtime = session_agent_def(app, sid)
        by_id = resolve_session_agent(app, "", session_id=sid)

    assert runtime.source == "plugin"
    assert runtime.id == route["agent"]["id"] == "standard"
    assert runtime.metadata["agent_ref"] == "clio/standard"
    assert runtime.metadata["agent_ref"] in listed
    assert runtime.metadata["resolution_digest"] == route["digest"]
    assert runtime.metadata["plugins"] == [row["id"] for row in route["plugins"]]
    assert runtime.tools == route["tool_names"]
    assert runtime.system_prompt == route["prompt"]["text"]
    assert by_id is not None and by_id.model_dump() == runtime.model_dump()


def test_a_live_delta_reaches_the_route_and_the_runtime_alike(tmp_path: Path) -> None:
    """#2: removing a plugin inside the session changes BOTH views identically."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = _session(client)
        before = session_agent_def(app, sid)
        patched = client.patch(f"/v1/sessions/{sid}/agent", json={"remove": ["shell"]})
        assert patched.status_code == 200, patched.text
        route = client.get(f"/v1/sessions/{sid}/agent").json()["resolution"]
        after = session_agent_def(app, sid)

    assert "shell_bash" in before.tools
    assert after.tools == route["tool_names"]
    assert "shell_bash" not in after.tools
    assert after.metadata["resolution_digest"] == route["digest"]
    assert after.metadata["resolution_digest"] != before.metadata["resolution_digest"]


def test_single_resolution_seam_serves_agent_and_defined_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3: one function resolves the session agent (by id or ref) and its subagents."""

    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"review": _REVIEW_PLUGIN},
        agents={"reviewer.py": _REVIEWER_AGENT},
    )
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = _session(client, agent_ref="clio/reviewer")
        resolution = session_resolution(app, sid)
        by_id = resolve_session_agent(app, "reviewer", session_id=sid)
        by_ref = resolve_session_agent(app, "clio/reviewer", session_id=sid)
        child = resolve_session_agent(app, "critic", session_id=sid)
        stranger = resolve_session_agent(app, "standard", session_id=sid)

    assert [row["id"] for row in resolution.subagents] == ["critic"]
    assert by_id is not None and by_ref is not None
    assert by_id.model_dump() == by_ref.model_dump()
    assert by_id.id == "reviewer" and by_id.tier == 1
    assert child is not None
    assert child.id == "critic"
    assert child.tier == 2
    assert child.parent_id == "clio/reviewer"
    assert "Critique the draft." in child.system_prompt
    # An installed agent the session did not select is not one of its agents.
    assert stranger is None


def test_empty_tool_allowlist_zeroes_the_session_agent(tmp_path: Path) -> None:
    """#4: an explicit empty ``tool_allowlist`` strips every tool, on every lookup."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        zero = _session(client, tool_allowlist=[])
        listed = _session(client, tool_allowlist=["fs_read_file"])
        plain = _session(client)
        zeroed = session_agent_def(app, zero)
        resolved = resolve_session_agent(app, "", session_id=zero)
        kept = session_agent_def(app, listed)
        untouched = session_agent_def(app, plain)

    for agent in (zeroed, resolved):
        assert agent is not None
        assert agent.tools == []
        assert agent.skills == []
        assert agent.module == {"kind": "chain_of_thought"}
        assert agent.id == "standard"
    # Only the explicit empty list zeroes; a non-empty or absent allowlist is a no-op.
    assert kept.tools == untouched.tools
    assert "shell_bash" in untouched.tools
    assert untouched.module == {"kind": "react"}


def test_apply_session_tool_allowlist_ignores_unknown_sessions(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    agent = AgentDef(id="x", title="X", source="plugin", tools=["fs_read_file"])
    assert apply_session_tool_allowlist(app, agent, session_id="") is agent
    assert apply_session_tool_allowlist(app, agent, session_id="sess_missing") is agent


class _ForwardSpyAgent:
    """Stand-in host agent whose legacy ``forward`` must NEVER be invoked (#948 S4b).

    The Tier-1 planner ``ClioAgent.forward`` is deleted; this spy proves the turn
    engine never falls back to a legacy ``app.state.agent.forward`` dispatch.
    """

    def __init__(self) -> None:
        self.forward_calls = 0

    def forward(self, *_args: object, **_kwargs: object) -> object:  # pragma: no cover
        self.forward_calls += 1
        raise AssertionError("legacy planner forward must not run (#948 S4b)")


def test_session_runs_its_agent_and_fails_typed_on_an_inexecutable_host(
    tmp_path: Path,
) -> None:
    """A session runs its resolved plugin agent -- never nothing, never a legacy planner.

    This host fake carries no tool executor, so the runtime genuinely cannot
    execute the standard agent's gateway tools: the turn must fail TYPED at
    module build (``not_implemented`` / ``custom_agent_tool_executor_unavailable``)
    -- a structured error turn, never an EMPTY assistant message, and never the
    deleted legacy planner ``forward`` (spy count stays 0).
    """

    spy = _ForwardSpyAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=spy)
    with TestClient(app) as client:
        from .conftest import complete_turn

        sid = _session(client)
        assistant = complete_turn(client, sid, "do something")

    error_info = assistant.get("error_info") or {}
    assert error_info.get("error") == "not_implemented", error_info
    details = error_info.get("details") or {}
    assert details.get("agent_id") == "standard"
    assert details.get("reason") == "custom_agent_tool_executor_unavailable"
    assert details.get("unsupported_tools"), "the agent's gateway tools are named"
    assert spy.forward_calls == 0


def test_session_whose_pinned_resolution_is_gone_fails_agent_unavailable(
    tmp_path: Path,
) -> None:
    """A broken pin stays TYPED: no substitute agent runs in its place."""

    spy = _ForwardSpyAgent()
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=spy)
    with TestClient(app) as client:
        from .conftest import complete_turn

        sid = _session(client)
        app.state.sessions.update(sid, metadata_patch={"agent_resolution": "0" * 64})
        route = client.get(f"/v1/sessions/{sid}/agent")
        assistant = complete_turn(client, sid, "do something")

    assert route.status_code == 422
    assert route.json()["error"]["error"] == "agent_resolution_missing"
    error_info = assistant.get("error_info") or {}
    assert error_info.get("error") == "agent_unavailable", error_info
    details = error_info.get("details") or {}
    assert details.get("reason") == "agent_resolution_missing"
    assert details.get("agent_ref") == "clio/standard"
    assert "choose_another_agent" in details.get("recovery_actions", [])
    assert resolve_session_agent(app, "", session_id=sid) is None
    assert spy.forward_calls == 0


def test_resolution_agent_def_is_the_executable_view(tmp_path: Path) -> None:
    """The runtime's ``AgentDef`` is derived from the resolution, nothing else."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = _session(client)
        resolution = session_resolution(app, sid)
    agent = resolution_agent_def(resolution)
    assert agent.tools == resolution.tool_names
    assert agent.skills == [row["id"] for row in resolution.skills]
    assert agent.metadata["mcp_namespaces"] == sorted(resolution.mcp_servers)
    assert agent.metadata["flow"]["id"] == "react"
    assert agent.tier == 1 and agent.parent_id == ""
