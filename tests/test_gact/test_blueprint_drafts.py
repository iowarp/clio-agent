"""Saved drafts never modify the applied snapshot; publishing detects source conflicts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from clio_agent.gact.agent_blueprints import install_agent_blueprint
from clio_agent.gact.blueprint_drafts import draft_view, publish_draft, read_draft, save_draft


@pytest.fixture
def blueprint(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "experts").mkdir(parents=True)
    (source / "AGENT.md").write_text(
        "---\nid: demo\nversion: 1.0.0\ntitle: Demo\nroot_expert: main\n---\nOriginal\n",
        encoding="utf-8",
    )
    (source / "experts" / "main.md").write_text(
        "---\nid: main\ntitle: Main\ntier: 1\nmodule:\n  kind: react\nprompt_id: demo.main\n---\nCoordinate.\n",
        encoding="utf-8",
    )
    result = install_agent_blueprint(source=str(source), scope="workspace", cwd=tmp_path)
    return source, Path(result["installed"][0]["root"])


def test_save_invalid_draft_leaves_runtime_and_source_intact(blueprint: tuple[Path, Path]) -> None:
    source, root = blueprint
    before = (root / "AGENT.md").read_text()
    result = save_draft(root, "AGENT.md", "---\ntitle: Missing identity\n---\nDraft")
    assert not result["validation"]["enabled"]
    assert (root / "AGENT.md").read_text() == before
    assert (source / "AGENT.md").read_text() == before
    assert draft_view(root) != root
    with pytest.raises(ValueError, match="Invalid draft"):
        publish_draft(root)


def test_publish_requires_separate_reload(blueprint: tuple[Path, Path]) -> None:
    source, root = blueprint
    before = (root / "AGENT.md").read_text()
    edited = before.replace("Original", "Published source")
    assert save_draft(root, "AGENT.md", edited)["validation"]["enabled"]
    result = publish_draft(root)
    assert result["published"] == ["AGENT.md"]
    assert result["reload_required"]
    assert (source / "AGENT.md").read_text() == edited
    assert (root / "AGENT.md").read_text() == before
    assert publish_draft(root)["published"] == []


def test_upstream_conflict_is_detected_before_any_file_write(blueprint: tuple[Path, Path]) -> None:
    source, root = blueprint
    before = (root / "AGENT.md").read_text()
    save_draft(root, "AGENT.md", before.replace("Original", "Draft"))
    expert = (root / "experts" / "main.md").read_text()
    save_draft(root, "experts/main.md", expert + "Draft instruction.\n")
    (source / "experts" / "main.md").write_text(expert + "External instruction.\n")
    with pytest.raises(ValueError, match="upstream_conflict"):
        publish_draft(root)
    assert (source / "AGENT.md").read_text() == before


def test_stale_editor_cannot_overwrite_newer_saved_draft(blueprint: tuple[Path, Path]) -> None:
    _, root = blueprint
    before = (root / "AGENT.md").read_text()
    old_hash = hashlib.sha256((root / "AGENT.md").read_bytes()).hexdigest()
    save_draft(root, "AGENT.md", before + "First edit.\n", expected_hash=old_hash)
    with pytest.raises(ValueError, match="draft_conflict"):
        save_draft(root, "AGENT.md", before + "Stale edit.\n", expected_hash=old_hash)
    assert (draft_view(root) / "AGENT.md").read_text().endswith("First edit.\n")


def test_same_name_drafts_from_other_marketplaces_remain_separate(
    blueprint: tuple[Path, Path], tmp_path: Path
) -> None:
    source, root = blueprint
    import shutil

    other_source = tmp_path / "other-source"
    shutil.copytree(source, other_source)
    other = Path(
        install_agent_blueprint(source=str(other_source), scope="workspace", cwd=tmp_path)[
            "installed"
        ][0]["root"]
    )
    before = (root / "AGENT.md").read_text()
    save_draft(root, "AGENT.md", before + "First marketplace draft.\n")
    assert draft_view(other) == other
    assert (draft_view(root) / "AGENT.md").read_text().endswith("First marketplace draft.\n")


def test_clean_editor_tracks_source_and_dirty_saved_draft_preserves_conflict(
    blueprint: tuple[Path, Path],
) -> None:
    source, root = blueprint
    before = (root / "AGENT.md").read_text()
    (source / "AGENT.md").write_text(before.replace("Original", "External version"))
    read = read_draft(root, "AGENT.md")
    assert "External version" in read["content"]
    save_draft(root, "AGENT.md", read["content"] + "My draft\n", expected_hash=read["content_hash"])
    (source / "AGENT.md").write_text(before.replace("Original", "Second external version"))
    assert "My draft" in read_draft(root, "AGENT.md")["content"]
    with pytest.raises(ValueError, match="upstream_conflict"):
        publish_draft(root)
    assert (root / "AGENT.md").read_text() == before


def test_workspaces_have_independent_authoring_drafts(
    blueprint: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source, root = blueprint
    other_workspace = tmp_path / "second-workspace"
    other_workspace.mkdir()
    other_root = Path(
        install_agent_blueprint(source=str(source), scope="workspace", cwd=other_workspace)[
            "installed"
        ][0]["root"]
    )
    save_draft(root, "AGENT.md", (root / "AGENT.md").read_text() + "First workspace draft\n")
    assert draft_view(other_root) == other_root
