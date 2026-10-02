"""``scripts/bump_provider_sdks.py``: the weekly bump picks shippable latest SDKs.

Driven by the recorded PyPI JSON in ``tests/test_providers/fixtures/pypi``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "test_providers" / "fixtures" / "pypi"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bump_provider_sdks", REPO_ROOT / "scripts" / "bump_provider_sdks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bump = _load()


def _payloads() -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in FIXTURES.glob("*.json")}


def test_groups_mirror_the_runtime_registry() -> None:
    from clio_agent.providers.components.registry import PROVIDER_COMPONENTS

    assert {
        k: (v.distributions, v.release_notes_url) for k, v in PROVIDER_COMPONENTS.items()
    } == bump.GROUPS


def test_bundle_platforms_cover_every_bundle_target() -> None:
    spec = importlib.util.spec_from_file_location(
        "cbl_for_bump", REPO_ROOT / "scripts" / "check_bundle_matches_lock.py"
    )
    assert spec is not None and spec.loader is not None
    cbl = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cbl
    spec.loader.exec_module(cbl)
    assert set(bump.BUNDLE_WHEEL_PLATFORMS) == set(cbl.BUNDLE_TARGETS)


def test_a_release_without_a_windows_wheel_is_never_shipped() -> None:
    """SABOTAGE: count any wheel -> 0.2.160 (no win_amd64) wins -> red."""
    versions = bump.shippable_versions(_payloads()["claude-agent-sdk"])
    assert "0.2.160" not in versions and "0.2.157" not in versions
    assert max(versions, key=bump.release_key) == "0.2.159"


def test_plan_bumps_both_groups_to_their_shippable_latest() -> None:
    floors = {"openai-codex-cli-bin": "0.147.0", "claude-agent-sdk": "0.2.156"}
    assert bump.plan(_payloads(), floors) == {
        "openai-codex-cli-bin": "0.157.1",
        "claude-agent-sdk": "0.2.159",
    }


def test_plan_is_empty_when_current() -> None:
    floors = {"openai-codex-cli-bin": "0.157.1", "claude-agent-sdk": "0.2.159"}
    assert bump.plan(_payloads(), floors) == {}


def test_floors_are_read_and_rewritten_in_the_real_pyproject() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floors = bump.current_floors(pyproject)
    assert set(floors) == {"openai-codex-cli-bin", "claude-agent-sdk"}
    rewritten = bump.rewrite_floors(pyproject, {"openai-codex-cli-bin": "9.9.9"})
    assert bump.current_floors(rewritten)["openai-codex-cli-bin"] == "9.9.9"
    assert bump.current_floors(rewritten)["claude-agent-sdk"] == floors["claude-agent-sdk"]
    assert rewritten.count('"openai-codex-cli-bin>=9.9.9"') == 1


def test_describe_links_release_notes() -> None:
    title, body = bump.describe({"claude-agent-sdk": "0.2.159"}, {"claude-agent-sdk": "0.2.156"})
    assert title == "chore(deps): bump provider SDKs to claude-agent-sdk 0.2.159"
    assert "| `claude-agent-sdk` (claude_code) | 0.2.156 | 0.2.159 |" in body
    assert "https://github.com/anthropics/claude-agent-sdk-python/releases" in body


def test_check_mode_writes_github_outputs_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    original = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (project / "pyproject.toml").write_text(original, encoding="utf-8")
    output = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(bump, "fetch", lambda name: _payloads()[name])
    monkeypatch.setattr(sys, "argv", ["bump", "--check", "--project", str(project)])
    assert bump.main() == 0
    written = output.read_text(encoding="utf-8")
    assert "changed=true" in written
    assert "body<<__CLIO_BODY__" in written
    assert (project / "pyproject.toml").read_text(encoding="utf-8") == original


def test_the_workflow_is_weekly_dispatchable_and_triggers_ci() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "provider-sdk-bump.yml").read_text(
        encoding="utf-8"
    )
    assert "schedule:" in workflow and "workflow_dispatch" in workflow
    assert "python scripts/bump_provider_sdks.py" in workflow
    assert "gh workflow run ci.yml" in workflow
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in ci
