"""SkillCatalog discovery + typed resolution (#916 S1 / #917).

Covers the scope precedence contract (a resolved plugin's skills shadow the
workspace, which shadows global), every typed failure mode (missing / ambiguous /
unreadable — never a silent skip), the ``skill_resolution`` diagnostic, skill
slash commands, and a shipped marketplace plugin's skills resolving end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.skills import (
    SkillBodyUnreadableError,
    SkillCatalog,
    read_skill_body,
)
from tests._marketplace import MARKETPLACE_ROOT


def _write_skill(root: Path, skill_id: str, *, body: str, layout: str = "skill_md") -> Path:
    """Author a skill file under `root` in either supported layout."""

    text = f"---\nname: {skill_id}\ndescription: {skill_id} description\n---\n\n{body}\n"
    if layout == "skill_md":
        path = root / skill_id / "SKILL.md"
    else:
        path = root / f"{skill_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home + cwd + plugin layout, with the catalog pointed at them."""

    home = tmp_path / "home"
    cwd = tmp_path / "work"
    pack = tmp_path / "pack"
    for base in (home / ".claude" / "skills", cwd / ".claude" / "skills", pack / "skills"):
        base.mkdir(parents=True)
    return {"home": home, "cwd": cwd, "pack": pack}


def _catalog(scopes: dict[str, Path], *, plugin: bool = False) -> SkillCatalog:
    """The catalog over ``scopes``; ``plugin`` adds the ``pack`` plugin's ``skills/``."""

    plugin_skills = [("pack", str(scopes["pack"] / "skills"))] if plugin else []
    return SkillCatalog(home=scopes["home"], cwd=scopes["cwd"], plugin_skills=plugin_skills)


# ---- precedence --------------------------------------------------------------


def test_plugin_shadows_workspace_shadows_global(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["home"] / ".claude" / "skills", "review", body="GLOBAL BODY")
    _write_skill(scopes["cwd"] / ".claude" / "skills", "review", body="WORKSPACE BODY")
    _write_skill(scopes["pack"] / "skills", "review", body="PACK BODY")

    with_pack = _catalog(scopes, plugin=True).resolve("review")
    assert with_pack.status == "resolved"
    assert with_pack.skill is not None and with_pack.skill.scope == "plugin"
    assert with_pack.skill.source == "pack"
    assert read_skill_body(with_pack.skill) == "PACK BODY"

    without_pack = _catalog(scopes).resolve("review")
    assert without_pack.status == "resolved"
    assert without_pack.skill is not None and without_pack.skill.scope == "workspace"
    assert read_skill_body(without_pack.skill) == "WORKSPACE BODY"


def test_global_resolves_when_no_higher_scope(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["home"] / ".claude" / "skills", "only-global", body="G")
    res = _catalog(scopes, plugin=True).resolve("only-global")
    assert res.status == "resolved"
    assert res.skill is not None and res.skill.scope == "global"


def test_flat_md_layout_resolves(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["cwd"] / ".claude" / "skills", "flat", body="F", layout="flat_md")
    res = _catalog(scopes).resolve("flat")
    assert res.status == "resolved"
    assert res.skill is not None and res.skill.layout == "flat_md"


# ---- typed failure modes ------------------------------------------------------


def test_missing_is_typed_not_silent(scopes: dict[str, Path]) -> None:
    res = _catalog(scopes, plugin=True).resolve("nope")
    assert res.status == "missing"
    assert res.skill is None
    assert "nope" in res.detail
    meta = res.to_metadata()
    assert meta["status"] == "missing" and meta["id"] == "nope"


def test_empty_id_is_missing(scopes: dict[str, Path]) -> None:
    assert _catalog(scopes).resolve("").status == "missing"
    assert _catalog(scopes).resolve("   ").status == "missing"


def test_same_tier_duplicate_is_ambiguous(scopes: dict[str, Path]) -> None:
    """Two definitions of one id inside ONE scope tier — typed ambiguity,
    while cross-tier duplicates are legal shadowing (precedence test above)."""

    codex_root = scopes["cwd"] / ".codex" / "skills"
    codex_root.mkdir(parents=True)
    _write_skill(scopes["cwd"] / ".claude" / "skills", "dup", body="A")
    _write_skill(codex_root, "dup", body="B")
    res = _catalog(scopes).resolve("dup")
    assert res.status == "ambiguous"
    assert len(res.candidates) == 2
    assert "dup" in res.detail
    assert sorted(res.to_metadata()["candidates"]) == sorted(res.candidates)


def test_unreadable_is_typed(scopes: dict[str, Path]) -> None:
    """A skill file that exists but cannot be decoded resolves `unreadable`,
    never `missing` and never a silent skip."""

    bad = scopes["cwd"] / ".claude" / "skills" / "broken" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xff\xfe\x00\x00garbage\x80\x81")
    catalog = _catalog(scopes)
    res = catalog.resolve("broken")
    assert res.status == "unreadable"
    assert res.candidates == (str(bad),)
    assert catalog.scan_errors and catalog.scan_errors[0]["path"] == str(bad)


def test_nonstring_name_is_typed_scan_error(scopes: dict[str, Path]) -> None:
    """A list-valued `name:` must not str()-coerce into a garbage id."""

    bad = scopes["cwd"] / ".claude" / "skills" / "listy" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\nname:\n- foo\n- bar\n---\n\nBody.\n", encoding="utf-8")
    catalog = _catalog(scopes)
    assert all("['foo'" not in ref.id for ref in catalog.discover())
    assert any("non-string frontmatter name" in e["error"] for e in catalog.scan_errors)


def test_unreadable_blocks_precedence_walk(scopes: dict[str, Path]) -> None:
    """A corrupt higher-tier definition surfaces as `unreadable`; it must NOT
    silently fall through to a lower-tier definition of the same id."""

    bad = scopes["pack"] / "skills" / "review" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xff\xfe\x00garbage")
    _write_skill(scopes["cwd"] / ".claude" / "skills", "review", body="GOOD LOWER TIER")
    res = _catalog(scopes, plugin=True).resolve("review")
    assert res.status == "unreadable"
    assert res.candidates == (str(bad),)


def test_cwd_equals_home_scopes_stay_explicit(tmp_path: Path) -> None:
    """cwd == home (daemon started from $HOME): scopes are declared per root,
    never inferred from path containment, so resolution still works and the
    coinciding roots dedup to one candidate instead of a false ambiguity."""

    base = tmp_path / "same"
    (base / ".claude" / "skills").mkdir(parents=True)
    _write_skill(base / ".claude" / "skills", "solo", body="S")
    res = SkillCatalog(home=base, cwd=base).resolve("solo")
    assert res.status == "resolved"
    assert res.skill is not None and res.skill.scope == "workspace"


def test_read_skill_body_raises_typed_when_file_vanishes(scopes: dict[str, Path]) -> None:
    path = _write_skill(scopes["cwd"] / ".claude" / "skills", "gone", body="X")
    res = _catalog(scopes).resolve("gone")
    assert res.status == "resolved" and res.skill is not None
    path.unlink()
    with pytest.raises(SkillBodyUnreadableError) as excinfo:
        read_skill_body(res.skill)
    assert excinfo.value.skill_id == "gone"


# ---- resolve_declared + agent-row diagnostic ----------------------------------


def test_resolve_declared_mixed_outcomes(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["pack"] / "skills", "have", body="H")
    out = _catalog(scopes, plugin=True).resolve_declared(["have", "have-not", "have"])
    assert list(out) == ["have", "have-not"]  # deduped, order preserved
    assert out["have"].status == "resolved"
    assert out["have-not"].status == "missing"


def test_resolved_metadata_carries_provenance(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["pack"] / "skills", "prov", body="P")
    meta = _catalog(scopes, plugin=True).resolve("prov").to_metadata()
    assert meta["status"] == "resolved"
    assert meta["scope"] == "plugin"
    assert meta["source"] == "pack"
    assert Path(meta["path"]).name == "SKILL.md"
    assert len(meta["checksum"]) == 64  # sha256 hex


def test_resolve_declared_metadata_reports_resolved_and_missing(
    scopes: dict[str, Path],
) -> None:
    """The ``skill_resolution`` diagnostic: resolved + missing are both visible."""

    _write_skill(scopes["pack"] / "skills", "real-skill", body="R")
    resolution = _catalog(scopes, plugin=True).resolve_declared_metadata(
        ["real-skill", "ghost-skill"]
    )
    assert resolution["real-skill"]["status"] == "resolved"
    assert resolution["real-skill"]["scope"] == "plugin"
    assert resolution["real-skill"]["source"] == "pack"
    assert resolution["ghost-skill"]["status"] == "missing"
    assert "ghost-skill" in resolution["ghost-skill"]["detail"]


def test_two_plugins_with_one_skill_id_are_ambiguous(scopes: dict[str, Path]) -> None:
    """Two resolved plugins shipping the same skill id are one tier: typed ambiguity."""

    other = scopes["pack"].parent / "other"
    _write_skill(scopes["pack"] / "skills", "shared", body="A")
    _write_skill(other / "skills", "shared", body="B")
    catalog = SkillCatalog(
        home=scopes["home"],
        cwd=scopes["cwd"],
        plugin_skills=[("pack", str(scopes["pack"] / "skills")), ("other", str(other / "skills"))],
    )
    res = catalog.resolve("shared")
    assert res.status == "ambiguous"
    assert len(res.candidates) == 2


def test_skill_command_rows_derive_from_catalog(
    scopes: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skill frontmatter command declarations still surface as slash commands,
    dispatching to `main` with the skill body as the template (#918); skills
    without command declarations produce no rows (parity with the old agents)."""

    from clio_agent.gact.runtime.commands import command_defs_from_skill
    from clio_agent.gact.skills import SkillCatalog

    root = scopes["cwd"] / ".claude" / "skills"
    (root / "cmdskill").mkdir(parents=True)
    (root / "cmdskill" / "SKILL.md").write_text(
        "---\nname: cmdskill\ndescription: D\nslash_command: /run-review\n---\n\nDo the review.\n",
        encoding="utf-8",
    )
    _write_skill(root, "plain-skill", body="No command keys here.")

    refs = {r.id: r for r in SkillCatalog(home=scopes["home"], cwd=scopes["cwd"]).discover()}
    rows = command_defs_from_skill(refs["cmdskill"])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "/run-review"
    assert row["agent_id"] == "main"
    assert row["agent_source"] == "skill"
    assert row["prompt_template"] == "Do the review."
    assert command_defs_from_skill(refs["plain-skill"]) == []


def test_skill_command_template_composes_with_body(
    scopes: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared template never REPLACES the skill body: the body is the
    procedure and always reaches the model (composed after the template)."""

    from clio_agent.gact.runtime.commands import command_defs_from_skill
    from clio_agent.gact.skills import SkillCatalog

    root = scopes["cwd"] / ".claude" / "skills" / "tpl"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: tpl\ndescription: D\ncommand: /tpl\n"
        "prompt-template: Explain {{input}}\n---\n\nTHE PROCEDURE.\n",
        encoding="utf-8",
    )
    refs = {r.id: r for r in SkillCatalog(home=scopes["home"], cwd=scopes["cwd"]).discover()}
    row = command_defs_from_skill(refs["tpl"])[0]
    assert row["prompt_template"].startswith("Explain {{input}}")
    assert "THE PROCEDURE." in row["prompt_template"]


def test_workspace_skill_command_shadows_global(
    scopes: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same skill id in global and workspace: the WORKSPACE definition's command
    wins (precedence parity with the deleted skills-as-agents fold)."""

    from clio_agent.gact.app import build_app
    from clio_agent.gact.runtime.commands import user_command_rows

    for base, marker in ((scopes["home"], "GLOBAL BODY"), (scopes["cwd"], "WORKSPACE BODY")):
        d = base / ".claude" / "skills" / "shared"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: shared\ndescription: D\ncommand: /shared\n---\n\n{marker}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: scopes["home"]))
    app = build_app(sessions_path=scopes["cwd"] / "s.json")
    rows = {r["id"]: r for r in user_command_rows(app, cwd=scopes["cwd"])}
    assert "WORKSPACE BODY" in rows["/shared"]["prompt_template"]
    assert "GLOBAL BODY" not in rows["/shared"]["prompt_template"]


# ---- marketplace fixtures ------------------------------------------------------


@pytest.mark.marketplace
def test_marketplace_data_semantics_plugin_skills_resolve(tmp_path: Path) -> None:
    """The shipped plugin resolves end-to-end: every skill the data-semantics
    plugin ships exists plugin-locally and resolves with plugin scope."""

    skills_root = MARKETPLACE_ROOT / "plugins" / "data-semantics" / "skills"
    catalog = SkillCatalog(
        home=tmp_path / "no-home",
        cwd=tmp_path / "no-cwd",
        plugin_skills=[("data-semantics", str(skills_root))],
    )
    declared = [
        "route-dataset-questions",
        "inspect-dataset-structure",
        "compare-variables",
        "reason-about-quality",
        "recommend-visual-checks",
    ]
    out = catalog.resolve_declared(declared)
    for skill_id, res in out.items():
        assert res.status == "resolved", f"{skill_id}: {res.detail}"
        assert res.skill is not None and res.skill.scope == "plugin"
        assert res.skill.source == "data-semantics"
        assert read_skill_body(res.skill).strip(), f"{skill_id} body is empty"
