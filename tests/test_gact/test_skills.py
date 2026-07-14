"""SkillCatalog discovery + typed resolution (#916 S1 / #917).

Covers the three-scope precedence contract (pack shadows workspace shadows
global), every typed failure mode (missing / ambiguous / unreadable — never a
silent skip), the ``skill_resolution`` agent-row diagnostic recorded by
``parse_expert_file``, the ``/skills/`` scanner exclusion in ``_expert_files``
(a loose pack SKILL.md must not materialize an expert), and the shipped
marketplace fixtures resolving end-to-end.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.expert_packs import (
    ExpertPackDefinition,
    _expert_files,
    parse_expert_file,
)
from clio_agent.gact.skills import (
    SkillBodyUnreadableError,
    SkillCatalog,
    read_skill_body,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


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
    """A home + cwd + pack layout, with the catalog pointed at them."""

    home = tmp_path / "home"
    cwd = tmp_path / "work"
    pack = tmp_path / "pack"
    for base in (home / ".claude" / "skills", cwd / ".claude" / "skills", pack / "skills"):
        base.mkdir(parents=True)
    return {"home": home, "cwd": cwd, "pack": pack}


def _catalog(scopes: dict[str, Path]) -> SkillCatalog:
    return SkillCatalog(home=scopes["home"], cwd=scopes["cwd"])


# ---- precedence --------------------------------------------------------------


def test_pack_shadows_workspace_shadows_global(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["home"] / ".claude" / "skills", "review", body="GLOBAL BODY")
    _write_skill(scopes["cwd"] / ".claude" / "skills", "review", body="WORKSPACE BODY")
    _write_skill(scopes["pack"] / "skills", "review", body="PACK BODY")

    with_pack = _catalog(scopes).resolve("review", pack_root=scopes["pack"])
    assert with_pack.status == "resolved"
    assert with_pack.skill is not None and with_pack.skill.scope == "pack"
    assert read_skill_body(with_pack.skill) == "PACK BODY"

    without_pack = _catalog(scopes).resolve("review")
    assert without_pack.status == "resolved"
    assert without_pack.skill is not None and without_pack.skill.scope == "workspace"
    assert read_skill_body(without_pack.skill) == "WORKSPACE BODY"


def test_global_resolves_when_no_higher_scope(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["home"] / ".claude" / "skills", "only-global", body="G")
    res = _catalog(scopes).resolve("only-global", pack_root=scopes["pack"])
    assert res.status == "resolved"
    assert res.skill is not None and res.skill.scope == "global"


def test_flat_md_layout_resolves(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["cwd"] / ".claude" / "skills", "flat", body="F", layout="flat_md")
    res = _catalog(scopes).resolve("flat")
    assert res.status == "resolved"
    assert res.skill is not None and res.skill.layout == "flat_md"


# ---- typed failure modes ------------------------------------------------------


def test_missing_is_typed_not_silent(scopes: dict[str, Path]) -> None:
    res = _catalog(scopes).resolve("nope", pack_root=scopes["pack"])
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
    res = _catalog(scopes).resolve("review", pack_root=scopes["pack"])
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
    out = SkillCatalog(home=scopes["home"], cwd=scopes["cwd"]).resolve_declared(
        ["have", "have-not", "have"], pack_root=scopes["pack"]
    )
    assert list(out) == ["have", "have-not"]  # deduped, order preserved
    assert out["have"].status == "resolved"
    assert out["have-not"].status == "missing"


def test_resolved_metadata_carries_provenance(scopes: dict[str, Path]) -> None:
    _write_skill(scopes["pack"] / "skills", "prov", body="P")
    meta = (
        _catalog(scopes).resolve("prov", pack_root=scopes["pack"]).to_metadata()
    )
    assert meta["status"] == "resolved"
    assert meta["scope"] == "pack"
    assert Path(meta["path"]).name == "SKILL.md"
    assert len(meta["checksum"]) == 64  # sha256 hex


def _expert_md(tmp_path: Path, *, skills: list[str]) -> Path:
    lines = "\n".join(f"  - {s}" for s in skills)
    path = tmp_path / "experts" / "analyst.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "id: analyst\n"
        "title: Analyst\n"
        "tier: 2\n"
        "parent_id: root\n"
        f"skills:\n{lines}\n"
        "---\n\nAnalyze things.\n",
        encoding="utf-8",
    )
    return path


def test_parse_expert_file_records_skill_resolution(
    scopes: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row-level diagnostic: resolved + missing are both visible, and a
    missing skill does NOT disable the expert (diagnostic, not validation
    error — the `prompt_resolution` pattern)."""

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: scopes["home"]))
    monkeypatch.setattr(os, "getcwd", lambda: str(scopes["cwd"]))
    _write_skill(scopes["pack"] / "skills", "real-skill", body="R")
    pack = ExpertPackDefinition(
        id="testpack",
        version="1",
        title="Test Pack",
        description="",
        scope="workspace",
        root=scopes["pack"],
    )
    row = parse_expert_file(
        _expert_md(scopes["pack"], skills=["real-skill", "ghost-skill"]),
        scope="workspace",
        pack=pack,
    )
    resolution: dict[str, Any] = row.metadata["skill_resolution"]
    assert resolution["real-skill"]["status"] == "resolved"
    assert resolution["real-skill"]["scope"] == "pack"
    assert resolution["ghost-skill"]["status"] == "missing"
    assert row.enabled, "a missing skill is a diagnostic, not a disabling error"
    assert all("skill" not in err for err in row.validation_errors)


def test_no_skills_declared_means_no_resolution_key(
    scopes: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: scopes["home"]))
    monkeypatch.setattr(os, "getcwd", lambda: str(scopes["cwd"]))
    path = scopes["pack"] / "experts" / "plain.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nid: plain\ntitle: Plain\ntier: 2\nparent_id: root\n---\n\nBody.\n",
        encoding="utf-8",
    )
    row = parse_expert_file(path, scope="workspace", pack=None)
    assert "skill_resolution" not in row.metadata


def test_load_expert_packs_resolves_against_passed_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace tier of skill_resolution follows the cwd passed to
    load_expert_packs (the session workspace), NOT the daemon process cwd."""

    from clio_agent.gact.expert_packs import load_expert_packs

    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    _write_skill(workspace / ".claude" / "skills", "ws-skill", body="W")
    pack_dir = workspace / ".clio" / "experts"
    pack_dir.mkdir(parents=True)
    (pack_dir / "analyst.md").write_text(
        "---\nid: ws-analyst\ntitle: A\ntier: 1\nskills:\n  - ws-skill\n---\n\nBody.\n",
        encoding="utf-8",
    )
    # Daemon cwd is elsewhere and has no such skill.
    monkeypatch.chdir(tmp_path)
    rows = {row.id: row for row in load_expert_packs(home=home, cwd=workspace)}
    resolution = rows["ws-analyst"].metadata["skill_resolution"]["ws-skill"]
    assert resolution["status"] == "resolved"
    assert resolution["scope"] == "workspace"


# ---- the /skills/ scanner exclusion -------------------------------------------


def test_pack_skill_md_is_not_an_expert(tmp_path: Path) -> None:
    """The loose-scan mis-parse: a SKILL.md under skills/ must never be picked
    up as an expert markdown file."""

    root = tmp_path / "pack"
    _write_skill(root / "skills", "rubric", body="RUBRIC")
    expert = root / "experts" / "real.md"
    expert.parent.mkdir(parents=True)
    expert.write_text("---\nid: real\ntier: 1\n---\n\nBody.\n", encoding="utf-8")
    files = _expert_files(root)
    assert expert in files
    assert all("/skills/" not in p.as_posix() for p in files)


# ---- marketplace fixtures ------------------------------------------------------


@pytest.mark.skipif(
    not (_REPO_ROOT / "external" / "clio-agent-marketplace" / "data-semantics").is_dir(),
    reason="marketplace submodule not checked out",
)
def test_marketplace_data_semantics_skills_resolve(tmp_path: Path) -> None:
    """The shipped fixtures resolve end-to-end: every skill the data-semantics
    experts declare exists pack-locally and resolves with pack scope."""

    pack_root = _REPO_ROOT / "external" / "clio-agent-marketplace" / "data-semantics"
    catalog = SkillCatalog(home=tmp_path / "no-home", cwd=tmp_path / "no-cwd")
    declared = [
        "route_dataset_questions",
        "inspect_dataset_structure",
        "compare_variables",
        "reason_about_quality",
        "recommend_visual_checks",
    ]
    out = catalog.resolve_declared(declared, pack_root=pack_root)
    for skill_id, res in out.items():
        assert res.status == "resolved", f"{skill_id}: {res.detail}"
        assert res.skill is not None and res.skill.scope == "pack"
        assert read_skill_body(res.skill).strip(), f"{skill_id} body is empty"
