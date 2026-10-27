"""An installed product fetches the default marketplace at the checkout's pinned commit.

Without a checkout (a wheel or the desktop runtime) the marketplace is cloned. The
repository's default branch is not what this release was built against, so the clone
uses the commit the checkout pins as its submodule.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.plugins import service

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("git") is None, reason="reads the submodule pin from git")
def test_the_pinned_commit_is_the_checkouts_submodule_pin() -> None:
    listing = subprocess.run(
        ["git", "ls-tree", "HEAD", "external/clio-agent-marketplace"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0 or not listing.stdout.strip():
        pytest.skip("not a git checkout with the marketplace submodule")
    pinned = listing.stdout.split()[2]
    assert service.DEFAULT_MARKETPLACE_COMMIT == pinned


class _Store:
    def __init__(self, installed: dict[str, Any] | None = None) -> None:
        self.installed = installed
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def marketplaces(self) -> dict[str, Any]:
        return {service.DEFAULT_MARKETPLACE: self.installed} if self.installed else {}

    def install(self, source: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((source, kwargs))
        return {"source": source}


def _service(store: _Store) -> Any:
    owner = object.__new__(service.PluginService)
    owner.store = store  # type: ignore[attr-defined]
    return owner


def test_a_clone_fetches_the_pinned_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service, "default_marketplace_source", lambda: service.DEFAULT_MARKETPLACE_URL
    )
    store = _Store()
    _service(store).ensure_default_marketplace()
    assert store.calls == [
        (
            service.DEFAULT_MARKETPLACE_URL,
            {"commit": service.DEFAULT_MARKETPLACE_COMMIT, "name": "clio", "default": True},
        )
    ]


def test_an_install_at_the_pin_is_not_fetched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service, "default_marketplace_source", lambda: service.DEFAULT_MARKETPLACE_URL
    )
    store = _Store(
        {"source": service.DEFAULT_MARKETPLACE_URL, "commit": service.DEFAULT_MARKETPLACE_COMMIT}
    )
    assert _service(store).ensure_default_marketplace() is None
    assert store.calls == []


def test_an_install_at_another_commit_moves_to_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service, "default_marketplace_source", lambda: service.DEFAULT_MARKETPLACE_URL
    )
    store = _Store({"source": service.DEFAULT_MARKETPLACE_URL, "commit": "0" * 40})
    _service(store).ensure_default_marketplace()
    assert store.calls[0][1]["commit"] == service.DEFAULT_MARKETPLACE_COMMIT
