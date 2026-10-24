"""GitHub guidance is a plugin prompt fragment gated on the shell tool actually resolving.

The guidance used to be a runtime snippet appended by the react loop whenever a
shell tool was bound. It is now the marketplace shell plugin's ``shell.prefer-gh``
fragment (``when: {tools: [shell_bash]}``), composed into the agent's prompt by
:mod:`clio_agent.plugins.compose` only when ``shell_bash`` resolves, and it reaches
every provider's request through the composed system prompt.
"""

from __future__ import annotations

from pathlib import Path

import dspy
import pytest

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.plugins.compose import fragment_applies
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.records import FragmentRecord
from clio_agent.plugins.resolve import Delta
from clio_agent.plugins.service import PluginService
from tests._marketplace import MARKETPLACE_ROOT
from tests._plugin_fixtures import custom_marketplace, fragment, tools_module
from tests._scripted_engine import Reply, scripted_lm

GUIDANCE = "Use the GitHub CLI `gh` through the normal shell tool; exclude drafts."

GH_AGENT = """
from clio_agent.sdk import Agent


class GitHubAgent(Agent):
    id = "gh-agent"
    version = "1.0.0"
    name = "Repo"
    role = "a repository expert"
    plugins = Agent.plugins + ["github", "shell"]
"""


def _marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginService:
    """A marketplace whose ``github`` plugin carries guidance gated on ``shell_bash``.

    ``github`` provides no tool itself, so its fragment can only apply when another
    plugin (``shell``) makes ``shell_bash`` resolve.
    """

    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "github": {
                "files": {
                    "ai.iowarp.clio/prompt/gh.md": fragment(
                        "github", "prefer-gh", "capabilities", GUIDANCE, "{tools: [shell_bash]}"
                    )
                }
            }
        },
        agents={"gh.py": GH_AGENT},
    )
    return PluginService()


def _fragment_rows(resolution) -> list[tuple[str, str, str]]:
    return [
        (row["section"], row["id"], row["plugin"])
        for section in resolution.prompt["sections"]
        for row in section["fragments"]
    ]


def test_fragment_applies_only_to_the_named_tool_or_its_namespace() -> None:
    gated = FragmentRecord(
        id="shell.prefer-gh",
        section="capabilities",
        body=GUIDANCE,
        path="",
        when_tools=("shell_bash",),
    )
    assert fragment_applies(gated, ["shell_bash", "fs_read_file"], ["core", "shell"])
    assert not fragment_applies(gated, ["connected_data_status", "fs_read_file"], ["core"])
    # A shell plugin that resolved without its bash tool does not get the guidance.
    assert not fragment_applies(gated, ["prepare_execution_runtime"], ["core", "shell"])


def test_shell_fragment_is_composed_only_when_shell_bash_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _marketplace(tmp_path, monkeypatch)
    with_shell = service.resolve("clio/gh-agent", pin=False)
    assert "shell_bash" in {row["name"] for row in with_shell.tools}
    assert ("capabilities", "github.prefer-gh", "github") in _fragment_rows(with_shell)
    assert GUIDANCE in with_shell.prompt_text

    without_shell = service.resolve("clio/gh-agent", delta=Delta(remove=("shell",)), pin=False)
    # The github plugin stays; only the tool its fragment needs is gone.
    assert "github" in without_shell.plugin_ids
    assert "shell_bash" not in {row["name"] for row in without_shell.tools}
    assert "github.prefer-gh" not in {row[1] for row in _fragment_rows(without_shell)}
    assert GUIDANCE not in without_shell.prompt_text


@pytest.mark.usefixtures("clio_core_plane")
@pytest.mark.parametrize("provider", ["codex", "claude_code", "vllm"])
def test_composed_guidance_reaches_the_model_request(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolution = _marketplace(tmp_path, monkeypatch).resolve("clio/gh-agent", pin=False)

    def shell_bash(command: str) -> str:
        """Run a shell command."""
        raise AssertionError("This test only observes the system prompt, never executes")

    lm, engine = scripted_lm([Reply(text="Ready.")])
    lm.model = f"{provider}/fixture"
    with dspy.context(lm=lm):
        ClioReAct("system_prompt, question -> answer", tools=[dspy.Tool(shell_bash)])(
            system_prompt=resolution.prompt_text, question="Check the newest release."
        )
    request = engine.requests[0]
    assert request.model == f"{provider}/fixture"
    assert isinstance(request.system, str)
    assert request.system.startswith("You are Repo, a repository expert.")
    assert GUIDANCE in request.system
    assert {tool.name for tool in request.tools} == {"shell_bash", "submit"}


def test_shell_plugin_without_a_bash_tool_gets_no_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragment gates on the tool, not on the plugin that ships it."""
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "shell": {
                "files": {
                    "ai.iowarp.clio/tools/builtin.py": tools_module("prepare_execution_runtime"),
                    "ai.iowarp.clio/prompt/gh.md": fragment(
                        "shell", "prefer-gh", "capabilities", GUIDANCE, "{tools: [shell_bash]}"
                    ),
                }
            }
        },
    )
    resolution = PluginService().resolve("clio/standard", pin=False)
    assert "shell" in resolution.plugin_ids
    assert "shell_bash" not in {row["name"] for row in resolution.tools}
    assert GUIDANCE not in resolution.prompt_text


@pytest.mark.marketplace
def test_marketplace_shell_plugin_ships_the_gated_gh_fragment() -> None:
    record = load_plugin(MARKETPLACE_ROOT / "plugins" / "shell", trusted=False)
    fragments = {row.id: row for row in record.fragments}
    prefer_gh = fragments["shell.prefer-gh"]
    assert prefer_gh.section == "capabilities"
    assert prefer_gh.when_tools == ("shell_bash",)
    body = " ".join(prefer_gh.body.split())
    assert "GitHub CLI (`gh`) through the shell" in body
    assert "github_cli" not in body
