"""Tests for ``scripts/compile_model_overlay.py`` (model-capabilities brief Part 8).

Covers: the schema accepts every real seed file and rejects a malformed entry,
the compiled output is deterministic, the staleness check fails on a stale (or
missing) committed JSON, and the adjacent claude-code-models.json schema check.
Uses a small fixture tree (mirroring ``test_check_file_size.py``'s approach)
for the isolated cases, and the real repo tree for the "does the actual commit
pass" cases.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from scripts import compile_model_overlay as c

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Against the REAL repo tree: this is the actual CI gate.
# --------------------------------------------------------------------------- #


def test_real_seed_files_all_validate_against_the_schema() -> None:
    schema = c._load_schema(c.OVERLAY_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    files = c._family_files()
    assert len(files) == 14
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = list(validator.iter_errors(raw))
        assert not errors, f"{path.name}: {[e.message for e in errors]}"


def test_real_repo_compiled_overlay_is_up_to_date() -> None:
    """The exact CI gate: ``--check`` against the real committed tree."""
    assert c.main(["--check"]) == 0


def test_real_claude_code_catalog_validates() -> None:
    problems = c._validate_catalog_if_present(
        c.CLAUDE_CODE_CATALOG_PATH, c.CLAUDE_CODE_SCHEMA_PATH, label="claude-code catalog"
    )
    assert problems == []


def test_real_codex_catalog_is_absent_and_therefore_skipped() -> None:
    """S1's codex-models.json has not landed on this branch yet -- must no-op, not fail."""
    assert not c.CODEX_CATALOG_PATH.exists()
    problems = c._validate_catalog_if_present(
        c.CODEX_CATALOG_PATH, c.CODEX_SCHEMA_PATH, label="codex catalog"
    )
    assert problems == []


# --------------------------------------------------------------------------- #
# Schema rejects a malformed entry (isolated -- does not touch the repo tree).
# --------------------------------------------------------------------------- #


def test_schema_rejects_an_entry_missing_match_patterns() -> None:
    schema = c._load_schema(c.OVERLAY_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors([{"family": "bad", "capabilities": {"chat": True}}]))
    assert errors
    assert any("matchPatterns" in e.message for e in errors)


def test_schema_rejects_an_entry_with_empty_match_patterns_list() -> None:
    schema = c._load_schema(c.OVERLAY_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    errors = list(
        validator.iter_errors(
            [{"family": "bad", "matchPatterns": [], "capabilities": {"chat": True}}]
        )
    )
    assert errors


def test_schema_rejects_a_capabilities_missing_required_fields() -> None:
    schema = c._load_schema(c.OVERLAY_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    errors = list(
        validator.iter_errors([{"family": "bad", "matchPatterns": ["bad"], "capabilities": {}}])
    )
    assert errors


# --------------------------------------------------------------------------- #
# Isolated fixture tree: determinism + staleness + duplicate-family rejection.
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every module path constant at a throwaway tree with two valid seed files."""
    models_dir = tmp_path / "catalogs" / "models"
    models_dir.mkdir(parents=True)
    shutil.copy(c.OVERLAY_SCHEMA_PATH, models_dir / "overlay.schema.json")
    (models_dir / "alpha.yaml").write_text(
        "- family: alpha\n"
        "  matchPatterns: [alpha]\n"
        "  capabilities:\n"
        "    chat: true\n"
        "    tools: true\n"
        "    reasoning: false\n"
        "    structuredOutputs: json-schema\n"
        "    vision: false\n"
        "    audio: false\n"
        "    embeddings: false\n"
        "    rerank: false\n"
        "    fim: false\n"
        "    contextWindow: 8192\n"
        "    maxTokens: 2048\n",
        encoding="utf-8",
    )
    (models_dir / "beta.yaml").write_text(
        "- family: beta\n"
        "  matchPatterns: [beta]\n"
        "  capabilities:\n"
        "    chat: true\n"
        "    tools: false\n"
        "    reasoning: false\n"
        "    structuredOutputs: json-schema\n"
        "    vision: false\n"
        "    audio: false\n"
        "    embeddings: false\n"
        "    rerank: false\n"
        "    fim: false\n"
        "    contextWindow: 4096\n"
        "    maxTokens: 1024\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(c, "MODELS_DIR", models_dir)
    monkeypatch.setattr(c, "OVERLAY_SCHEMA_PATH", models_dir / "overlay.schema.json")
    monkeypatch.setattr(c, "COMPILED_OVERLAY_PATH", tmp_path / "catalogs" / "model-overlay.json")
    monkeypatch.setattr(
        c,
        "BUNDLED_OVERLAY_PATH",
        tmp_path / "bundled" / "model-overlay.json",
    )
    monkeypatch.setattr(
        c, "CLAUDE_CODE_CATALOG_PATH", tmp_path / "catalogs" / "claude-code-models.json"
    )
    monkeypatch.setattr(
        c, "CLAUDE_CODE_SCHEMA_PATH", tmp_path / "catalogs" / "claude-code-models.schema.json"
    )
    monkeypatch.setattr(c, "CODEX_CATALOG_PATH", tmp_path / "catalogs" / "codex-models.json")
    monkeypatch.setattr(c, "CODEX_SCHEMA_PATH", tmp_path / "catalogs" / "codex-models.schema.json")
    return tmp_path


def test_compile_output_is_deterministic(isolated_tree: Path) -> None:
    entries_a = c.load_and_validate_entries()
    entries_b = c.load_and_validate_entries()
    rendered_a = c.render(c.compile_overlay(entries_a))
    rendered_b = c.render(c.compile_overlay(entries_b))
    assert rendered_a == rendered_b
    # And sorted by family, regardless of the (alpha, beta) file-load order.
    doc = json.loads(rendered_a)
    assert [e["family"] for e in doc["entries"]] == ["alpha", "beta"]


def test_main_writes_both_the_canonical_and_bundled_copies(isolated_tree: Path) -> None:
    assert c.main([]) == 0
    assert c.COMPILED_OVERLAY_PATH.exists()
    assert c.BUNDLED_OVERLAY_PATH.exists()
    assert c.COMPILED_OVERLAY_PATH.read_text() == c.BUNDLED_OVERLAY_PATH.read_text()


def test_check_fails_when_compiled_json_was_never_written(isolated_tree: Path) -> None:
    assert not c.COMPILED_OVERLAY_PATH.exists()
    assert c.main(["--check"]) == 1


def test_check_passes_right_after_a_fresh_compile(isolated_tree: Path) -> None:
    assert c.main([]) == 0
    assert c.main(["--check"]) == 0


def test_check_fails_when_the_committed_json_is_stale(isolated_tree: Path) -> None:
    assert c.main([]) == 0
    # Simulate a hand-edited YAML file that nobody recompiled.
    (c.MODELS_DIR / "alpha.yaml").write_text(
        (c.MODELS_DIR / "alpha.yaml").read_text().replace("8192", "16384"),
        encoding="utf-8",
    )
    assert c.main(["--check"]) == 1


def test_check_fails_when_only_the_bundled_copy_is_stale(isolated_tree: Path) -> None:
    assert c.main([]) == 0
    c.BUNDLED_OVERLAY_PATH.write_text('{"schema_version": 1, "entries": []}\n', encoding="utf-8")
    assert c.main(["--check"]) == 1


def test_duplicate_family_across_two_files_is_rejected(isolated_tree: Path) -> None:
    (c.MODELS_DIR / "beta.yaml").write_text(
        (c.MODELS_DIR / "beta.yaml").read_text().replace("family: beta", "family: alpha"),
        encoding="utf-8",
    )
    with pytest.raises(c.OverlayCompileError, match="already defined"):
        c.load_and_validate_entries()


def test_malformed_yaml_entry_fails_main_not_an_exception(isolated_tree: Path) -> None:
    (c.MODELS_DIR / "broken.yaml").write_text(
        "- family: broken\n  capabilities: {}\n",  # missing matchPatterns
        encoding="utf-8",
    )
    assert c.main([]) == 1


def test_claude_code_catalog_schema_mismatch_fails_main(isolated_tree: Path) -> None:
    c.CLAUDE_CODE_SCHEMA_PATH.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["nope"],
            }
        ),
        encoding="utf-8",
    )
    c.CLAUDE_CODE_CATALOG_PATH.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert c.main([]) == 1


def test_claude_code_catalog_present_without_a_schema_fails_main(isolated_tree: Path) -> None:
    c.CLAUDE_CODE_CATALOG_PATH.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert not c.CLAUDE_CODE_SCHEMA_PATH.exists()
    assert c.main([]) == 1


def test_codex_catalog_absent_does_not_fail_main(isolated_tree: Path) -> None:
    assert not c.CODEX_CATALOG_PATH.exists()
    assert c.main([]) == 0
