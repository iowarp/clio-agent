"""Tests for the one-time codex -> chatgpt provider config migration (A.9)."""

from __future__ import annotations

from pathlib import Path

import yaml

from clio_agent.gact.chatgpt_provider_migration import (
    _migrate_one_file,
    migrate_codex_provider_configs,
)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_missing_file_reports_missing(tmp_path: Path) -> None:
    assert _migrate_one_file(tmp_path / "config.yaml") == "missing"


def test_no_lm_section_reports_no_reference(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_yaml(path, {"runtime": {"environment": "dev"}})
    assert _migrate_one_file(path) == "no_codex_reference"


def test_lm_provider_not_codex_reports_no_reference(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_yaml(path, {"lm": {"provider": "claude_code"}})
    assert _migrate_one_file(path) == "no_codex_reference"


def test_codex_provider_is_rewritten_to_chatgpt(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_yaml(path, {"lm": {"provider": "codex", "model": "gpt-5.5-codex"}})
    assert _migrate_one_file(path) == "migrated"
    rewritten = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert rewritten["lm"]["provider"] == "chatgpt"
    assert rewritten["lm"]["model"] == "gpt-5.5-codex"  # untouched


def test_codex_transport_is_rewritten_to_chatgpt_transport(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_yaml(path, {"lm": {"provider": "codex", "codex_transport": "sdk"}})
    _migrate_one_file(path)
    rewritten = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "codex_transport" not in rewritten["lm"]
    assert rewritten["lm"]["chatgpt_transport"] == "websocket"


def test_other_top_level_keys_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_yaml(path, {"lm": {"provider": "codex"}, "runtime": {"environment": "prod"}})
    _migrate_one_file(path)
    rewritten = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert rewritten["runtime"]["environment"] == "prod"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_yaml(path, {"lm": {"provider": "codex"}})
    assert _migrate_one_file(path) == "migrated"
    assert _migrate_one_file(path) == "no_codex_reference"


def test_empty_file_reports_no_reference(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert _migrate_one_file(path) == "no_codex_reference"


def test_unreadable_yaml_reports_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("not: valid: yaml: [", encoding="utf-8")
    assert _migrate_one_file(path) == "unreadable"


def test_non_mapping_yaml_reports_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert _migrate_one_file(path) == "unreadable"


def test_migrate_codex_provider_configs_touches_every_resolved_path(
    tmp_path: Path, monkeypatch
) -> None:
    workspace_config = tmp_path / "workspace" / ".clio" / "config.yaml"
    user_config = tmp_path / "user_config" / "config.yaml"
    _write_yaml(workspace_config, {"lm": {"provider": "codex"}})
    _write_yaml(user_config, {"lm": {"provider": "codex"}})

    import clio_agent.gact.chatgpt_provider_migration as migration_module

    monkeypatch.setattr(
        migration_module, "config_file_paths", lambda: (user_config, workspace_config)
    )
    result = migrate_codex_provider_configs()
    assert result["migrated"] is True
    assert result["files"][str(user_config)] == "migrated"
    assert result["files"][str(workspace_config)] == "migrated"


def test_migrate_codex_provider_configs_never_raises_on_a_bad_file(
    tmp_path: Path, monkeypatch
) -> None:
    import clio_agent.gact.chatgpt_provider_migration as migration_module

    bad_path = tmp_path / "config.yaml"
    bad_path.write_text("[", encoding="utf-8")
    monkeypatch.setattr(migration_module, "config_file_paths", lambda: (bad_path,))
    result = migrate_codex_provider_configs()  # must not raise
    assert result["migrated"] is False
    assert result["files"][str(bad_path)] == "unreadable"
