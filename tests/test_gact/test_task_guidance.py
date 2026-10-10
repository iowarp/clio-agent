"""Task lifecycle guidance reaches the common model request for roots and children."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import dspy
import pytest

from clio_agent.gact.agent_blueprints import load_agent_blueprint_path
from clio_agent.gact.agents import task_guidance as guidance
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.task_tools import build_task_tools
from clio_agent.prompts import PromptRegistry, PromptSource
from tests._marketplace import MARKETPLACE_ROOT
from tests._scripted_engine import Reply, scripted_lm


@pytest.mark.usefixtures("clio_core_plane")
@pytest.mark.parametrize("provider", ["codex", "claude_code", "vllm"])
@pytest.mark.parametrize("role", ["root", "child"])
def test_task_lifecycle_reaches_the_model_request(provider: str, role: str) -> None:
    """Provider requests carry delivery/Stop semantics without mission-specific help."""
    lm, engine = scripted_lm([Reply(text="Ready.")])
    lm.model = f"{provider}/fixture"
    with dspy.context(lm=lm):
        ClioReAct("system_prompt, question -> answer", tools=build_task_tools())(
            system_prompt=f"You are a {role} data expert.", question="Prepare for work."
        )
    request = engine.requests[0]
    assert request.model == f"{provider}/fixture"
    assert isinstance(request.system, str)
    assert request.system.startswith(f"You are a {role} data expert.")
    assert "next model iteration" in request.system
    assert "a queued result starts a new\nturn" in request.system
    assert "without waiting\nfor another user message" in request.system
    assert "the result will wake you to continue" in request.system
    assert "Finish requested independent actions before yielding" in request.system
    assert "Automatic wake is not a reason to skip requested actions" in request.system
    assert "Their later results can wake you again, including after Stop" in request.system
    assert "Conversation Stop ends the turn and its waiter, leaving accepted tasks running" in (
        request.system
    )
    assert "Subagent, MCP, Download,\nIndexing" in request.system
    assert {tool.name for tool in request.tools} == {
        "query_tasks",
        "observe_tasks",
        "wait_tasks",
        "cancel_tasks",
        "get_task_result",
        "submit",
    }


def test_guidance_does_not_invent_task_controls() -> None:
    """An expert without shared controls is not told it can call them."""
    assert guidance.task_guidance(["fs_read_file", "shell_bash"]) == ""


def test_task_guidance_uses_the_editable_prompt_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace task instructions override the built-in snippet through its registry."""
    (tmp_path / "tasks.md").write_text(
        "---\nid: clio.runtime.tasks\nprofile: default\n---\nTasks outlive this turn.\n",
        encoding="utf-8",
    )
    registry = PromptRegistry(sources=[PromptSource("workspace", tmp_path)])
    monkeypatch.setattr(guidance, "PromptRegistry", lambda: registry)
    assert guidance.task_guidance(["query_tasks", "wait_tasks"]) == "Tasks outlive this turn."


def test_missing_task_guidance_is_an_explicit_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not silently omit the lifecycle contract when a configured snippet is missing."""
    registry = PromptRegistry(sources=[], builtins={})
    monkeypatch.setattr(guidance, "PromptRegistry", lambda: registry)
    with pytest.raises(ValueError, match="shared task lifecycle prompt"):
        guidance.task_guidance(["query_tasks", "wait_tasks"])


@pytest.mark.marketplace
@pytest.mark.usefixtures("clio_core_plane")
def test_all_shipped_react_experts_receive_shared_task_lifecycle(
    record_property: Callable[[str, object], None],
) -> None:
    """Real shipped root/child prompts retain domain policy and receive shared controls."""
    seen: set[tuple[str, str]] = set()
    controls = {"query_tasks", "observe_tasks", "wait_tasks", "cancel_tasks", "get_task_result"}
    for manifest in sorted(MARKETPLACE_ROOT.glob("*/AGENT.md")):
        for agent in load_agent_blueprint_path(manifest.parent):
            if not agent.enabled or agent.module.get("kind") != "react":
                continue
            tools = build_auto_react_tools(agent, a2ui_producers=False)
            assert controls <= {tool.name for tool in tools}
            lm, engine = scripted_lm([Reply(text="Ready.")])
            with dspy.context(lm=lm):
                ClioReAct("system_prompt, question -> answer", tools=tools)(
                    system_prompt=agent.system_prompt, question="Prepare for work."
                )
            system = engine.requests[0].system
            assert isinstance(system, str) and system.startswith(agent.system_prompt.strip())
            assert (
                "next model iteration" in system
                and "wake you again, including after Stop" in system
            )
            assert "Finish requested independent actions before yielding" in system
            seen.add((manifest.parent.name, agent.id))
    assert {
        ("base-agent", "base"),
        ("document-production", "main"),
        ("earthscope-gnss-region", "ndp_resource_resolver"),
        ("factorio", "main"),
        ("factorio-flat", "main"),
    } <= seen
    record_property("expert_count", len(seen))
    record_property("experts", ",".join(f"{pack}/{agent}" for pack, agent in sorted(seen)))
