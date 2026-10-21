"""Task lifecycle guidance is the core plugin's ``core.tasks`` fragment.

The fragment (``when: {tools: [query_tasks, wait_tasks]}``) is composed into the
capabilities section of every root agent and defined subagent whose resolved tools
include the shared task controls, and reaches each provider's request through the
composed system prompt. A subagent's tool allowlist never removes them (D11).
"""

from __future__ import annotations

from pathlib import Path

import dspy
import pytest

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.task_declarations import build_task_tools
from clio_agent.plugins.resolve import Resolution
from clio_agent.plugins.service import PluginService
from tests._plugin_fixtures import custom_marketplace
from tests._scripted_engine import Reply, scripted_lm

REVIEW_AGENT = """
from clio_agent.sdk import Agent


class ReviewingAgent(Agent):
    id = "reviewing"
    version = "1.0.0"
    name = "Reviewer"
    role = "a data review agent"
    plugins = Agent.plugins + ["workspace", "review"]
"""

SUBAGENTS = """
from clio_agent.sdk import Subagent

worker = Subagent("worker", prompt="You are a child data expert.", description="Does work.")
reader = Subagent(
    "reader",
    prompt="You only read files.",
    description="Reads files.",
    plugins=["workspace"],
    tools=["fs_read_file"],
)
"""

#: Phrases from the lifecycle contract every task-capable agent must receive.
LIFECYCLE = (
    "eligible messages and results enter before the next model call",
    "While idle, they wake you without another human message",
    "Instructions inside a Subagent's assignment belong to that child",
    "Finish the user's requested independent actions before yielding",
    "Automatic wake is not a reason to skip requested independent actions",
    "Apply the latest human request",
    "It does not copy transcripts or replay operations",
    "Conversation Stop ends your model execution and waiter",
    "Accepted background tasks continue and their results may wake you after Stop",
    "Stop also cancels a foreground shell command",
    "waits for its actual process-tree cleanup",
    "Subagent, Session, MCP, Download, Indexing, Shell and Question",
    'assignment_disposition="yield"',
    'assignment_disposition="complete"',
    "Ending a turn does not itself complete your assignment",
    "ask_user always posts the question and immediately returns a Question handle",
    "Stopping or expiring the wait leaves the question open",
    "Model-written intent grants nothing",
    "Peer collection never consumes the owner's delivery",
)


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginService:
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"review": {"files": {"ai.iowarp.clio/subagents/review.py": SUBAGENTS}}},
        agents={"reviewing.py": REVIEW_AGENT},
    )
    return PluginService()


def _capabilities(resolution: Resolution) -> dict[str, dict]:
    section = next(s for s in resolution.prompt["sections"] if s["section"] == "capabilities")
    return {row["id"]: row for row in section["fragments"]}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> Resolution:
    service = _service(tmp_path, monkeypatch)
    if role == "root":
        return service.resolve("clio/standard", pin=False)
    parent = service.resolve("clio/reviewing", pin=False)
    return service.resolve_subagent(parent, "worker", pin=False)


@pytest.mark.parametrize("role", ["root", "child"])
def test_task_capable_agent_carries_core_tasks_in_capabilities(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolution = _resolved(tmp_path, monkeypatch, role)
    tools = {row["name"] for row in resolution.tools}
    assert {"query_tasks", "wait_tasks"} <= tools
    fragments = _capabilities(resolution)
    assert fragments["core.tasks"]["plugin"] == "core"
    text = _flat(fragments["core.tasks"]["text"])
    for phrase in LIFECYCLE:
        assert phrase in text
    # Only the capabilities section carries it.
    for section in resolution.prompt["sections"]:
        if section["section"] != "capabilities":
            assert "core.tasks" not in {row["id"] for row in section["fragments"]}


def test_tool_allowlist_keeps_core_task_controls_and_their_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subagent allowlist narrows its plugins' tools; ``core``'s task controls stay (D11)."""

    service = _service(tmp_path, monkeypatch)
    parent = service.resolve("clio/reviewing", pin=False)
    reader = service.resolve_subagent(parent, "reader", pin=False)
    tools = {row["plugin"]: set() for row in reader.tools}
    for row in reader.tools:
        tools[row["plugin"]].add(row["name"])
    assert tools.pop("workspace") == {"fs_read_file"}
    assert set(tools) == {"core"} and {"query_tasks", "wait_tasks"} <= tools["core"]
    assert "core.tasks" in _capabilities(reader)
    assert reader.prompt_text.startswith("You only read files.")


@pytest.mark.usefixtures("clio_core_plane")
@pytest.mark.parametrize("provider", ["codex", "claude_code", "vllm"])
@pytest.mark.parametrize("role", ["root", "child"])
def test_task_lifecycle_reaches_the_model_request(
    provider: str, role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider requests carry delivery/Stop semantics through the composed prompt."""
    resolution = _resolved(tmp_path, monkeypatch, role)
    lm, engine = scripted_lm([Reply(text="Ready.")])
    lm.model = f"{provider}/fixture"
    with dspy.context(lm=lm):
        ClioReAct("system_prompt, question -> answer", tools=build_task_tools())(
            system_prompt=resolution.prompt_text, question="Prepare for work."
        )
    request = engine.requests[0]
    assert request.model == f"{provider}/fixture"
    assert isinstance(request.system, str)
    head = (
        "You are CLIO, an autonomous scientific coding and data assistant"
        if role == "root"
        else "You are a child data expert."
    )
    assert request.system.startswith(head)
    system = _flat(request.system)
    for phrase in LIFECYCLE:
        assert phrase in system
    assert {tool.name for tool in request.tools} == {
        "query_tasks",
        "observe_tasks",
        "wait_tasks",
        "cancel_tasks",
        "get_task_result",
        "pause_agent",
        "resume_agent",
        "restart_agent",
        "send_agent_message",
        "submit",
    }
