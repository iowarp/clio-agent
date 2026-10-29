"""Marketplace pin verification must never silently bypass (#772, Slice 7).

When a local marketplace source is installed, the store resolves its git commit
to check it against a caller-supplied ``commit`` pin. If that check cannot be
made the pin must NOT be silently skipped:

* pinned + unverifiable commit -> the install is refused (``source_unavailable``);
* unpinned + unresolvable commit -> best-effort install + a structured
  ``reason=...`` record (trace or the marketplace entry's diagnostics).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from clio_agent.plugins import sources
from clio_agent.plugins.store import PluginStore, StoreError
from tests.test_plugins.helpers import plugin, write


def _write_min_marketplace(root: Path, name: str = "pinned") -> Path:
    write(root / "ai.iowarp.clio" / "marketplace.json", json.dumps({"name": name}))
    plugin(root / "plugins", "notes")
    write(
        root / "agents" / "pinned.py",
        """
        from clio_agent.sdk import Agent


        class Pinned(Agent):
            id = "pinned-agent"
            version = "0.1.0"
            role = "a pinned marketplace agent"
            plugins = Agent.plugins + ["notes"]
        """,
    )
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_marketplace(root: Path) -> tuple[Path, str]:
    _write_min_marketplace(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "pinned marketplace",
    )
    return root, _git(root, "rev-parse", "HEAD")


def test_pinned_install_with_unverifiable_commit_is_refused(tmp_path: Path) -> None:
    source, _head = _git_marketplace(tmp_path / "src")
    store = PluginStore(tmp_path / "store")

    with pytest.raises(StoreError) as caught:
        store.install(str(source), commit="deadbeef" * 5)

    assert caught.value.code == "source_unavailable"
    assert "pinned" not in store.marketplaces(), "a refused pin must not install anything"


def test_pinned_install_of_the_matching_commit_records_the_pin(tmp_path: Path) -> None:
    source, head = _git_marketplace(tmp_path / "src")
    store = PluginStore(tmp_path / "store")

    store.install(str(source), commit=head)

    entry = store.marketplaces()["pinned"]
    assert entry["pinned"] is True
    assert entry["commit"] == head


def test_unpinned_install_with_unresolvable_commit_records_a_reason_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, _head = _git_marketplace(tmp_path / "src")
    real_git = sources._git

    def _rev_parse_fails(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
        if args[:1] == ["rev-parse"]:
            raise sources.SourceError("git rev-parse failed: fatal: not a git repository")
        return real_git(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(sources, "_git", _rev_parse_fails)
    store = PluginStore(tmp_path / "store")

    with caplog.at_level(logging.WARNING):
        summary = store.install(str(source))

    assert summary["marketplace"] == "pinned", "unpinned install should still succeed"
    entry = store.marketplaces()["pinned"]
    logged = any("reason=" in record.getMessage() for record in caplog.records)
    recorded = any("commit" in str(row) for row in entry.get("diagnostics") or [])
    assert logged or recorded, "an unresolvable commit on an unpinned install must emit a reason"
