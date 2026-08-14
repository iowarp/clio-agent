"""iowarp/clio-agent#1211: the built-in ``/update-models`` skill.

The skill carries NO logic (pure prose instructing the refresh action + the
delta report) -- mirrors ``tests/test_gact/test_planning_skill.py``'s discovery
assertions for the other built-in skill.
"""

from __future__ import annotations

from clio_agent.gact.skills import SkillCatalog, read_skill_body


def _catalog() -> SkillCatalog:
    # The conftest isolation keeps clio's shipped built-in root, so a normally-
    # built catalog resolves the built-in `update-models` skill with no
    # per-test authoring needed (same pattern as test_planning_skill.py).
    return SkillCatalog()


def test_update_models_skill_is_discovered_as_builtin() -> None:
    res = _catalog().resolve("update-models")
    assert res.status == "resolved"
    assert res.skill is not None
    assert res.skill.scope == "builtin"


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


def test_refresh_provider_models_tool_the_skill_names_actually_exists() -> None:
    """The skill's instructed tool name must match a REAL auto-attached tool
    (#1211 review R6) -- never a dangling reference."""
    from clio_agent.gact.agents.auto_tools import build_auto_react_tools
    from clio_agent.gact.types import AgentDef

    agent_def = AgentDef(id="main", source="expert_pack", title="Main", metadata={})
    tools = build_auto_react_tools(agent_def)
    names = {getattr(t, "name", "") for t in tools}
    assert "refresh_provider_models" in names
