"""iowarp/clio-agent#1211: the ``fleet`` plugin's ``/update-models`` skill.

The skill carries NO logic (pure prose instructing the refresh action + the
delta report) -- mirrors ``tests/test_gact/test_planning_skill.py``'s discovery
assertions for the ``core`` skill.
"""

from __future__ import annotations

from pathlib import Path

import clio_agent.plugins
from clio_agent.gact.skills import SkillCatalog, read_skill_body

FLEET_SKILLS = Path(clio_agent.plugins.__file__).resolve().parent / "builtin" / "fleet" / "skills"


def _catalog() -> SkillCatalog:
    # ``update-models`` ships in the fleet plugin, which core requires (every agent has it).
    return SkillCatalog(plugin_skills=[("fleet", str(FLEET_SKILLS))])


def test_update_models_skill_is_discovered_in_the_fleet_plugin() -> None:
    res = _catalog().resolve("update-models")
    assert res.status == "resolved"
    assert res.skill is not None
    assert res.skill.scope == "plugin"
    assert res.skill.source == "fleet"


def test_update_models_skill_declares_no_privileged_effect() -> None:
    """Pure instructional skill: no ``effect:`` frontmatter (unlike ``planning``)."""
    res = _catalog().resolve("update-models")
    assert res.skill is not None
    assert "effect" not in res.skill.meta


def test_update_models_skill_body_names_the_refresh_tool_and_report_contract() -> None:
    """#1211 review R6: the skill instructs calling the ``refresh_provider_models``
    agent tool (expert-pool-primary doctrine) -- not a curl at a guessed port."""
    res = _catalog().resolve("update-models")
    assert res.skill is not None
    body = read_skill_body(res.skill)
    assert "refresh_provider_models" in body
    assert "added" in body and "removed" in body and "unchanged" in body
    assert "failed_reason" in body
    assert "rejected" in body  # #1211 review N3: rejected reasons are relayed too
    assert "account/backend's discovered default" in body
    assert "cli_default" not in body


def test_refresh_provider_models_tool_is_claimed_by_fleet() -> None:
    """The skill's instructed tool name must match a REAL tool (#1211 review R6) that
    the same ``fleet`` plugin claims -- never a dangling reference."""
    from types import SimpleNamespace

    from clio_agent.gact.agents.native_tool_registry import build_native_tools
    from clio_agent.plugins.service import PluginService

    resolution = PluginService().resolve("clio/standard", pin=False)
    owners = {row["name"]: row["plugin"] for row in resolution.tools}
    assert owners["refresh_provider_models"] == "fleet"
    available, _gateway, withheld = build_native_tools(
        SimpleNamespace(id="standard", metadata={}), ["refresh_provider_models"], {}
    )
    assert available["refresh_provider_models"].name == "refresh_provider_models"
    assert withheld == {}
