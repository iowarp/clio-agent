"""GitHub guidance follows the declared shell, through the common provider request seam."""

from __future__ import annotations

from pathlib import Path

import dspy
import pytest

from clio_agent.gact.agents import github_shell_guidance as guidance
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.catalog import _builtin_tools
from clio_agent.gact.skills import SkillCatalog, read_skill_body
from clio_agent.gact.types import AgentDef
from clio_agent.prompts import PromptRegistry, PromptSource
from tests._scripted_engine import Reply, scripted_lm


@pytest.mark.usefixtures("clio_core_plane")
@pytest.mark.parametrize("provider", ["codex", "claude_code", "vllm"])
@pytest.mark.parametrize("shell_name", ["bash", "shell_bash"])
def test_shared_guidance_reaches_the_model_request(provider: str, shell_name: str) -> None:
    def shell(command: str) -> str:
        """Run a shell command."""
        raise AssertionError("This test only observes the system prompt, never executes a command")

    lm, engine = scripted_lm([Reply(text="Ready.")])
    lm.model = f"{provider}/fixture"
    with dspy.context(lm=lm):
        ClioReAct("system_prompt, question -> answer", tools=[dspy.Tool(shell, name=shell_name)])(
            system_prompt="You are a repository expert.", question="Check the newest release."
        )
    request = engine.requests[0]
    assert request.model == f"{provider}/fixture"
    assert isinstance(request.system, str)
    assert request.system.startswith("You are a repository expert.")
    assert "gh repo clone OWNER/REPO DESTINATION" in request.system
    assert "through the normal shell tool" in request.system
    assert "Exclude drafts" in request.system
    assert "newest published prerelease" in request.system
    assert {tool.name for tool in request.tools} == {shell_name, "submit"}


def test_guidance_does_not_invent_shell_access() -> None:
    assert guidance.github_shell_guidance(["connected_data_status", "fs_read_file"]) == ""


def test_shared_guidance_uses_the_editable_prompt_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "github.md").write_text(
        "---\nid: clio.runtime.github_shell\nprofile: default\n---\nUse the configured shell gh.\n",
        encoding="utf-8",
    )
    registry = PromptRegistry(sources=[PromptSource("workspace", tmp_path)])
    monkeypatch.setattr(guidance, "PromptRegistry", lambda: registry)
    assert guidance.github_shell_guidance(["shell_bash"]) == "Use the configured shell gh."


def test_missing_shared_prompt_is_an_explicit_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = PromptRegistry(sources=[], builtins={})
    monkeypatch.setattr(guidance, "PromptRegistry", lambda: registry)
    with pytest.raises(ValueError, match="shared GitHub shell prompt"):
        guidance.github_shell_guidance(["shell_bash"])


def test_root_and_child_catalogs_omit_the_github_wrapper() -> None:
    for parent_id in ("", "main"):
        agent = AgentDef(id="expert", title="Expert", source="expert_pack", parent_id=parent_id)
        names = {tool.name for tool in build_auto_react_tools(agent, a2ui_producers=False)}
        assert "github_cli" not in names
    catalog = {tool.name for tool in _builtin_tools()}
    assert "github_cli" not in catalog
    assert "shell_bash" in catalog
    skill = SkillCatalog().resolve("connect-data").skill
    assert skill is not None
    instructions = read_skill_body(skill)
    assert "use `gh`\nthrough the normal shell" in instructions
    assert "github_cli" not in instructions
