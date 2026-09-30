"""``a2ui-component-design`` skill: Claude Code and Codex copies must never drift.

Issue #1533 asks for the same skill under both agent surfaces — ``.claude/skills``
(Claude Code) and ``.agents/skills`` (Codex) — with a test that fails the moment one
copy is edited without the other. clio-schemas is the canonical source (the design
guidance in this file is authored there, once, in ``feat/a2ui-data-everywhere``);
clio-agent vendors its own two copies here and keeps them byte-identical to each
other via this test (gact-tui is expected to run the equivalent check for its own
vendored copies).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "a2ui-component-design" / "SKILL.md"
CODEX_SKILL_PATH = REPO_ROOT / ".agents" / "skills" / "a2ui-component-design" / "SKILL.md"


def test_skill_files_exist() -> None:
    assert CLAUDE_SKILL_PATH.is_file(), f"missing: {CLAUDE_SKILL_PATH}"
    assert CODEX_SKILL_PATH.is_file(), f"missing: {CODEX_SKILL_PATH}"


def test_claude_and_codex_skill_copies_are_byte_identical() -> None:
    """A drift between the two copies is a bug the moment either one is edited alone."""

    claude_bytes = CLAUDE_SKILL_PATH.read_bytes()
    codex_bytes = CODEX_SKILL_PATH.read_bytes()
    assert claude_bytes == codex_bytes, (
        f"{CLAUDE_SKILL_PATH} and {CODEX_SKILL_PATH} have drifted — edit one and copy "
        "it verbatim to the other."
    )


def test_skill_declares_its_frontmatter_name() -> None:
    """Sanity check: the file is a real skill doc, not an empty placeholder."""

    text = CLAUDE_SKILL_PATH.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: a2ui-component-design" in text
    assert "description:" in text


def test_skill_names_clio_schemas_as_the_canonical_copy() -> None:
    """clio-agent vendors this skill; clio-schemas is where it is authored."""

    text = CLAUDE_SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    assert "clio-schemas is the canonical copy" in frontmatter
