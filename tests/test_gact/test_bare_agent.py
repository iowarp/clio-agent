"""A bare agent (``Agent.bare``): its model sees only the conversation.

No system prompt (not even the signature's docstring), no tools or skills (not even
``submit``), no per-turn context injections, and every reply is the answer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.resolution import is_bare, session_agent_def
from clio_agent.gact.app import build_app
from clio_agent.sdk import Agent
from clio_agent.sdk.agent import CORE_PLUGIN, AgentDefinitionError, AgentSpec
from tests._plugin_fixtures import custom_marketplace
from tests._scripted_engine import Reply, scripted_lm, wire

_BAREBONE_AGENT = """
from clio_agent.sdk import Agent
from clio_agent.sdk.agent import CORE_PLUGIN


class Barebone(Agent):
    id = "barebone"
    version = "1.0.0"
    name = "Barebone"
    role = "a bare model"
    plugins = [CORE_PLUGIN]
    bare = True
"""


class _Bare(Agent):
    id = "bare-spec"
    plugins = [CORE_PLUGIN]
    bare = True


class _Plain(Agent):
    id = "plain-spec"
    plugins = [CORE_PLUGIN]


def test_bare_round_trips_and_leaves_other_checksums_alone() -> None:
    bare, plain = _Bare.spec(), _Plain.spec()

    assert bare.bare and not plain.bare
    assert AgentSpec.from_dict(bare.to_dict()) == bare
    assert AgentSpec.from_dict(plain.to_dict()) == plain
    # Only a bare spec carries the key: stored specs of every other agent keep loading.
    assert "bare" not in plain.body()
    assert bare.body()["bare"] is True
    # The flag is part of the checksum: a stored spec cannot be flipped silently.
    with pytest.raises(AgentDefinitionError, match="checksum mismatch"):
        AgentSpec.from_dict({**bare.to_dict(), "bare": False})


def _session(client: TestClient, **metadata: Any) -> str:
    resp = client.post("/v1/sessions", json={"title": "bare", "metadata": metadata})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_a_bare_agent_resolves_to_no_prompt_tools_or_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_marketplace(tmp_path, monkeypatch, agents={"barebone.py": _BAREBONE_AGENT})
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        bare = session_agent_def(app, _session(client, agent_ref="clio/barebone"))
        standard = session_agent_def(app, _session(client))
        listed = {row["id"]: row for row in client.get("/v1/agents").json()["agents"]}

    assert bare.id == "barebone" and is_bare(bare)
    assert (bare.system_prompt, bare.tools, bare.skills) == ("", [], [])
    assert bare.module == {"kind": "chain_of_thought"}
    # Every other agent is untouched.
    assert not is_bare(standard) and "bare" not in standard.metadata
    assert standard.system_prompt and standard.tools
    assert "barebone" in listed


@pytest.mark.usefixtures("clio_core_plane")
def test_a_bare_program_sends_no_system_message_and_no_tools() -> None:
    lm, engine = scripted_lm([Reply(text="ANSWER: 42")])
    program = ClioReAct("system_prompt, question -> answer", tools=[])
    object.__setattr__(program, "_clio_bare", True)
    object.__setattr__(program, "_clio_unlisted", frozenset({"submit"}))
    with dspy.context(lm=lm):
        pred = program(system_prompt="You are the data expert.", question="what?")

    request = engine.requests[0]
    assert request.system is None  # not the prompt, and not the signature's docstring
    assert not request.tools  # not even submit
    assert wire(request) == [("user", [("text", "what?")])]
    assert (pred.answer, pred.termination_reason) == ("ANSWER: 42", "direct_response")


@pytest.mark.usefixtures("clio_core_plane")
def test_a_plain_program_still_lists_submit_and_sends_its_prompt() -> None:
    lm, engine = scripted_lm([Reply(text="ok")])
    with dspy.context(lm=lm):
        ClioReAct("system_prompt, question -> answer", tools=[])(
            system_prompt="You are the data expert.", question="what?"
        )

    request = engine.requests[0]
    assert request.system == "You are the data expert."
    assert [tool.name for tool in request.tools] == ["submit"]
