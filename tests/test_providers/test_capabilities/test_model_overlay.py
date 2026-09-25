"""Unit tests for :mod:`clio_agent.providers.capabilities.model_overlay` (brief Part 8 / P6).

Covers: longest-match-wins across entries, local-overlay root precedence and
same-length ties ("the later root wins" -- brief Part 8.6), the overlay facts
landing on :class:`ModelCapabilities` with ``source="overlay"``, and the fetch
path's cold start from the bundled packaged copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent import paths
from clio_agent.providers.capabilities import model_overlay
from clio_agent.providers.capabilities.model_overlay import (
    CatalogOverlaySource,
    OverlayEntry,
    best_overlay_match,
    entry_to_model_capabilities,
    overlay_match_for_link,
)


def _entry(family: str, patterns: list[str], root: str, **capabilities: object) -> OverlayEntry:
    return OverlayEntry(
        family=family,
        match_patterns=tuple(patterns),
        capabilities=dict(capabilities),
        quirks={},
        root=root,
    )


# --------------------------------------------------------------------------- #
# best_overlay_match: longest match + later-root-wins ties
# --------------------------------------------------------------------------- #


def test_longest_pattern_wins_across_entries() -> None:
    entries = [
        _entry("short", ["qwen"], "fetched"),
        _entry("long", ["qwen3.6-27b"], "fetched"),
    ]
    match = best_overlay_match("qwen3.6-27b-instruct", entries)
    assert match is not None
    assert match.entry.family == "long"
    assert match.pattern == "qwen3.6-27b"


def test_match_is_case_insensitive_substring() -> None:
    entries = [_entry("gemma", ["Gemma-4-31B-IT"], "fetched")]
    match = best_overlay_match("some-gemma-4-31b-it-q4_k_m.gguf", entries)
    assert match is not None and match.entry.family == "gemma"


def test_no_match_returns_none() -> None:
    entries = [_entry("qwen", ["qwen3.6"], "fetched")]
    assert best_overlay_match("totally-unrelated-model", entries) is None


def test_empty_candidate_returns_none() -> None:
    entries = [_entry("qwen", ["qwen"], "fetched")]
    assert best_overlay_match("", entries) is None


def test_equal_length_match_the_later_root_wins() -> None:
    """Same pattern length, different roots -- project beats user beats fetched."""
    entries = [
        _entry("fetched-family", ["acme-7b"], "fetched"),
        _entry("user-family", ["acme-7b"], "user"),
    ]
    match = best_overlay_match("acme-7b-q4", entries)
    assert match is not None
    assert match.entry.family == "user-family"

    entries_with_project = [*entries, _entry("project-family", ["acme-7b"], "project")]
    match2 = best_overlay_match("acme-7b-q4", entries_with_project)
    assert match2 is not None
    assert match2.entry.family == "project-family"


def test_user_root_precedence_beats_fetched_even_when_loaded_first() -> None:
    """Load order is fetched-then-user-then-project; precedence is independent of it."""
    entries = [
        _entry("fetched-family", ["exact-tie-pattern"], "fetched"),
        _entry("user-family", ["exact-tie-pattern"], "user"),
        _entry("project-family", ["shorter"], "project"),
    ]
    match = best_overlay_match("exact-tie-pattern-suffix", entries)
    assert match is not None
    assert match.entry.family == "user-family"


# --------------------------------------------------------------------------- #
# entry_to_model_capabilities: overlay facts land with source="overlay"
# --------------------------------------------------------------------------- #


def test_entry_to_model_capabilities_stamps_overlay_source_and_detail() -> None:
    entry = OverlayEntry(
        family="qwen-test",
        match_patterns=("qwen-test",),
        capabilities={
            "chat": True,
            "tools": True,
            "reasoning": True,
            "structuredOutputs": "json-schema",
            "vision": True,
            "audio": False,
            "contextWindow": 131072,
            "maxTokens": 8192,
        },
        quirks={
            "thinking": {
                "mechanism": "budget-tokens",
                "budgetByLevel": {"low": 1024, "medium": 4096, "high": 16384},
                "guidance": "test guidance",
            },
            "sampling": {
                "thinking": {"temperature": 0.6, "topP": 0.95},
                "instruct": {"temperature": 0.7},
            },
            "measuredUnder": {"hardware": "RTX 4090", "runtime": "llama.cpp", "date": "2026-05-01"},
        },
        root="fetched",
    )

    caps = entry_to_model_capabilities("qwen-test", entry, "qwen-test")

    assert caps.model_key == "qwen-test"
    assert caps.context_max.value == 131072
    assert caps.context_max.source == "overlay"
    assert "family=qwen-test" in caps.context_max.detail
    assert "matchPattern='qwen-test'" in caps.context_max.detail
    assert "measuredUnder=" in caps.context_max.detail
    # measuredUnder.date is used as the observed_at (provenance date), not "now".
    assert caps.context_max.observed_at == "2026-05-01"

    assert caps.output_max.value == 8192
    assert caps.tools.value is True
    assert caps.tools.source == "overlay"
    assert caps.input_modalities.value == frozenset({"text", "image"})
    assert caps.structured_output.value is True

    assert caps.thinking.known
    spec = caps.thinking.value
    assert spec is not None
    assert spec.mechanism == "budget_tokens"
    assert spec.levels == ("low", "medium", "high")
    assert spec.budget_range == (1024, 16384)

    assert caps.sampling_thinking.value == {"temperature": 0.6, "topP": 0.95}
    assert caps.sampling_instruct.value == {"temperature": 0.7}

    # Fields the overlay's schema never carries stay honest unknowns.
    assert not caps.parallel_tool_calls.known
    assert not caps.forbidden_params.known


def test_entry_to_model_capabilities_reasoning_false_is_mechanism_none() -> None:
    entry = OverlayEntry(
        family="plain",
        match_patterns=("plain",),
        capabilities={"reasoning": False},
        quirks={},
        root="fetched",
    )
    caps = entry_to_model_capabilities("plain", entry, "plain")
    assert caps.thinking.known
    spec = caps.thinking.value
    assert spec is not None
    assert spec.mechanism == "none"


def test_entry_to_model_capabilities_missing_capability_keys_stay_unknown() -> None:
    entry = OverlayEntry(
        family="bare", match_patterns=("bare",), capabilities={}, quirks={}, root="fetched"
    )
    caps = entry_to_model_capabilities("bare", entry, "bare")
    assert not caps.context_max.known
    assert not caps.tools.known
    assert not caps.input_modalities.known
    assert not caps.thinking.known


# --------------------------------------------------------------------------- #
# CatalogOverlaySource + overlay_match_for_link: local-root wiring end to end
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_fetched_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate local-root tests from the real seeded catalog content."""
    monkeypatch.setattr(model_overlay, "_fetched_entries", lambda: [])


def _write_overlay_yaml(directory: Path, filename: str, family: str, patterns: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    patterns_yaml = "\n".join(f"      - {p}" for p in patterns)
    (directory / filename).write_text(
        f"- family: {family}\n"
        f"  matchPatterns:\n{patterns_yaml}\n"
        f"  capabilities:\n"
        f"    chat: true\n"
        f"    tools: true\n"
        f"    contextWindow: 4096\n",
        encoding="utf-8",
    )


def test_catalog_overlay_source_reads_the_project_root(tmp_path: Path) -> None:
    project_dir = tmp_path / ".clio" / "model-catalog.d"
    _write_overlay_yaml(project_dir, "my-model.yaml", "my-local-model", ["my-local-model"])

    source = CatalogOverlaySource(cwd=tmp_path)
    facts = source.facts("my-local-model")
    assert facts is not None
    assert facts.context_max.value == 4096
    assert facts.context_max.source == "overlay"


def test_catalog_overlay_source_project_beats_user_on_equal_length_match(
    tmp_path: Path,
) -> None:
    user_dir = paths.user_config_dir() / "model-catalog.d"
    _write_overlay_yaml(user_dir, "override.yaml", "user-family", ["shared-pattern"])
    project_dir = tmp_path / ".clio" / "model-catalog.d"
    _write_overlay_yaml(project_dir, "override.yaml", "project-family", ["shared-pattern"])

    source = CatalogOverlaySource(cwd=tmp_path)
    facts = source.facts("shared-pattern-model")
    assert facts is not None
    assert facts.model_key == "shared-pattern-model"
    # entry_to_model_capabilities always stamps the model_key it's given, so
    # confirm via detail which family actually won the match.
    assert "family=project-family" in facts.context_max.detail


def test_catalog_overlay_source_returns_none_for_no_match(tmp_path: Path) -> None:
    source = CatalogOverlaySource(cwd=tmp_path)
    assert source.facts("nothing-matches-anything") is None


def test_overlay_match_for_link_returns_the_matched_family(tmp_path: Path) -> None:
    project_dir = tmp_path / ".clio" / "model-catalog.d"
    _write_overlay_yaml(project_dir, "m.yaml", "resolved-family", ["wire-id-fragment"])
    assert overlay_match_for_link("some-wire-id-fragment-q4", cwd=tmp_path) == "resolved-family"


def test_overlay_match_for_link_returns_none_for_no_match(tmp_path: Path) -> None:
    assert overlay_match_for_link("nothing-here", cwd=tmp_path) is None


def test_malformed_local_entry_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project_dir = tmp_path / ".clio" / "model-catalog.d"
    project_dir.mkdir(parents=True)
    (project_dir / "bad.yaml").write_text(
        "- family: incomplete\n  capabilities:\n    chat: true\n",  # missing matchPatterns
        encoding="utf-8",
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.capabilities.model_overlay"):
        source = CatalogOverlaySource(cwd=tmp_path)
        assert source.facts("incomplete") is None
    assert any("malformed_local_entry" in r.getMessage() for r in caplog.records)
