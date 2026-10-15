"""The built-in ``hdf5`` skill: one routed entry, guides loaded on demand."""

from __future__ import annotations

import re
from pathlib import Path

from clio_agent.gact.agents.skill_runtime import (
    SkillRuntime,
    build_load_skill_tool,
    effective_declared_skills,
)
from clio_agent.gact.catalog import _builtin_main_agent
from clio_agent.gact.skills import SkillCatalog
from clio_agent.gact.types import AgentDef

SKILL_ID = "hdf5"
REFERENCE = re.compile(r"`references/([a-z0-9_]+\.md)`")


def _skill_dir(tmp_path: Path) -> Path:
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    resolution = catalog.resolve(SKILL_ID)
    assert resolution.skill is not None
    assert resolution.skill.scope == "builtin"
    return Path(resolution.skill.dir)


def test_default_main_declares_the_single_hdf5_entry(tmp_path: Path) -> None:
    """Plain chat lists one HDF5 skill, not one per topic."""
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    declared = effective_declared_skills(_builtin_main_agent(), catalog)
    assert SKILL_ID in declared
    assert not [skill for skill in declared if skill.startswith("hdf5_")]


def test_description_stays_short(tmp_path: Path) -> None:
    """The description is listed in every default chat's prompt."""
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    skill = catalog.resolve(SKILL_ID).skill
    assert skill is not None
    assert 0 < len(skill.description) <= 300


def test_router_and_guides_match(tmp_path: Path) -> None:
    """Every guide the router names exists, and every guide is named."""
    skill_dir = _skill_dir(tmp_path)
    named = set(REFERENCE.findall((skill_dir / "SKILL.md").read_text(encoding="utf-8")))
    shipped = {path.name for path in (skill_dir / "references").glob("*.md")}
    assert named == shipped


def test_guide_cross_references_resolve(tmp_path: Path) -> None:
    """Guides point at each other by file, never at retired per-topic skill ids."""
    skill_dir = _skill_dir(tmp_path)
    shipped = {path.name for path in (skill_dir / "references").glob("*.md")}
    for guide in (skill_dir / "references").glob("*.md"):
        text = guide.read_text(encoding="utf-8")
        assert set(REFERENCE.findall(text)) <= shipped, guide.name
        assert not re.findall(r"`hdf5_[a-z0-9_]+`", text), guide.name


def test_load_skill_returns_router_then_guides(tmp_path: Path) -> None:
    """An expert that declares the skill can load the router and several guides at once."""
    agent = AgentDef(
        id="analyst",
        title="Analyst",
        source="expert_pack",
        parent_id="main",
        skills=[SKILL_ID],
    )
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    runtime = SkillRuntime(resolutions=catalog.resolve_declared([SKILL_ID]))
    load_skill = build_load_skill_tool(agent, runtime).func

    router = load_skill(skill_id=SKILL_ID)
    assert "references/conventions.md" in router

    guides = load_skill(
        skill_id=SKILL_ID, files=["references/chunking.md", "references/filters.md"]
    )
    assert "Chunk" in guides
    assert "references/filters.md" in guides
