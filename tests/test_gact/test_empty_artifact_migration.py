"""Opening a fresh artifact index must work before any conversation exists."""

from pathlib import Path

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.registry import get_registry


def test_empty_store_opens_artifact_registry_on_first_access_and_later_boot(tmp_path: Path) -> None:
    first = build_app(sessions_path=tmp_path / "first.json")
    registry = get_registry(first)

    assert registry.count() == 0
    assert registry.get_by_artifact_id("artifact_missing") is None
    assert get_registry(first) is registry

    # A second app opens the durable empty marker written by the first access.
    later = build_app(sessions_path=tmp_path / "later.json")
    reopened = get_registry(later)
    assert reopened is not registry
    assert reopened.count() == 0
    assert reopened.get_by_artifact_id("artifact_missing") is None
