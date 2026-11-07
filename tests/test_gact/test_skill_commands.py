"""Every skill the session's agent has is a ``/<skill>`` command, loaded for the person."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._plugin_fixtures import custom_marketplace

pytestmark = pytest.mark.usefixtures("host_agent_executor")

AGENTS = """
from clio_agent.sdk import Agent


class Scientist(Agent):
    id = "scientist"
    version = "1.0.0"
    role = "a scientist"
    plugins = Agent.plugins + ["workspace", "science"]
"""


def _skill(name: str, body: str, extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {name} procedure.\n{extra}---\n{body}\n"


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Recorder:
    def __init__(self) -> None:
        self.questions: list[str] = []
        self.started = threading.Event()

    def forward(self, question: str = "", *args: Any, **kwargs: Any) -> _Pred:
        self.questions.append(question)
        self.started.set()
        return _Pred(answer="DONE")


@pytest.fixture()
def science(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "science": {
                "files": {
                    "skills/run-a-study/SKILL.md": _skill(
                        "run-a-study", "Step 1: write the question down.", "argument-hint: <q>\n"
                    ),
                    "skills/internal-notes/SKILL.md": _skill(
                        "internal-notes", "Agent only.", "user-invocable: false\n"
                    ),
                }
            }
        },
        agents={"science_agents.py": AGENTS},
    )
    return tmp_path


def _session(client: TestClient) -> str:
    response = client.post(
        "/v1/sessions", json={"title": "t", "metadata": {"agent_ref": "clio/scientist"}}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def test_the_agents_skills_are_commands_except_user_invocable_false(science: Path) -> None:
    with TestClient(build_app(sessions_path=science / "s.json", agent=_Recorder())) as c:
        sid = _session(c)
        rows = {
            r["id"]: r for r in c.get("/v1/commands", params={"session_id": sid}).json()["commands"]
        }
        study = rows["/run-a-study"]
        assert study["kind"] == "skill" and study["plugin"] == "science"
        assert study["qualified_id"] == "/science:run-a-study"
        assert study["argument_hint"] == "<q>"
        assert "/internal-notes" not in rows
        # Without a session there is no agent, so no skills.
        assert "/run-a-study" not in {r["id"] for r in c.get("/v1/commands").json()["commands"]}


def test_a_skill_command_loads_the_skill_for_the_person(
    science: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.runtime import globals as runtime_globals

    emitted: list[tuple[str, dict[str, Any]]] = []
    real = runtime_globals._emit_semantic_event

    def record(app: Any, sid: str, name: str, **kwargs: Any) -> Any:
        emitted.append((name, kwargs))
        return real(app, sid, name, **kwargs)

    monkeypatch.setattr(runtime_globals, "_emit_semantic_event", record)
    agent = _Recorder()
    with TestClient(build_app(sessions_path=science / "s.json", agent=agent)) as c:
        sid = _session(c)
        resp = c.post(
            f"/v1/sessions/{sid}/commands/run-a-study", json={"input": "does caching help?"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["type"] == "turn"
        deadline = time.monotonic() + 30
        while not agent.questions and time.monotonic() < deadline:
            time.sleep(0.05)
        (question,) = agent.questions
        assert question.startswith("does caching help?\n\n# Skill: run-a-study")
        assert "Step 1: write the question down." in question
        user = next(
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "user"
        )
        assert [p["text"] for p in user["parts"] if p["type"] == "text"] == [
            "/run-a-study does caching help?"
        ]
        assert user["metadata"]["loaded_skill"] == "run-a-study"
        assert user["metadata"]["command_kind"] == "skill"
        (load,) = [kw for name, kw in emitted if name == "skill.loaded"]
        assert load["actor"] == {"role": "person"}
        assert load["summary"] == "the person loaded skill run-a-study"
