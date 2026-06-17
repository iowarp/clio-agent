"""Prompt-alignment reference matrix guardrails."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "PROMPT_ALIGNMENT_REFERENCE_MATRIX.md"


def test_prompt_alignment_reference_matrix_covers_required_families() -> None:
    text = DOC.read_text(encoding="utf-8")
    for phrase in (
        "System identity",
        "Planner/router",
        "Hierarchical parent experts",
        "Child expert prompts",
        "Tool-use prompts",
        "Command prompts",
        "Skills/user-defined agents",
        "Memory/context prompts",
        "Compaction prompts",
        "Permission prompts",
        "Error/recovery prompts",
        "Ask-user/retry prompts",
        "Prompt profiles",
        "TUI-visible prompt behavior",
    ):
        assert phrase in text


def test_prompt_alignment_reference_matrix_uses_public_sources_only() -> None:
    text = DOC.read_text(encoding="utf-8").lower()
    assert "public, inspectable" in text
    for forbidden in ("internal-only", "unverifiable"):
        assert forbidden not in text


def test_prompt_alignment_reference_matrix_links_core_sources() -> None:
    text = DOC.read_text(encoding="utf-8")
    for link in (
        "https://github.com/openai/codex/blob/main/docs/agents_md.md",
        "https://docs.anthropic.com/en/docs/claude-code/slash-commands",
        "https://docs.anthropic.com/en/docs/claude-code/sub-agents",
        "https://modelcontextprotocol.io/docs/concepts/prompts",
        "https://modelcontextprotocol.io/docs/concepts/tools",
        "ARC_MEMORY_LAYER.md",
        "REAL_PROVIDER_SEMANTIC_REGRESSION.md",
    ):
        assert link in text
