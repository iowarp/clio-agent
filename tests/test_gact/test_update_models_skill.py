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


def test_update_models_skill_body_names_the_refresh_endpoint_and_report_contract() -> None:
    res = _catalog().resolve("update-models")
    assert res.skill is not None
    body = read_skill_body(res.skill)
    assert "/v1/providers/models/refresh" in body
    assert "added" in body and "removed" in body and "unchanged" in body
    assert "failed_reason" in body
