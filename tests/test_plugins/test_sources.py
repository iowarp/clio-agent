"""A marketplace file's git sources never reach ``git`` as options or local folders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.plugins import sources, store as store_module
from clio_agent.plugins.sources import FetchedSource, SourceError, fetch
from clio_agent.plugins.store import PluginStore
from tests.test_plugins.helpers import plugin, write


def _claude_market(root: Path, plugins: list[dict], federated: list[dict] | None = None) -> Path:
    write(
        root / ".claude-plugin" / "marketplace.json", json.dumps({"name": "m", "plugins": plugins})
    )
    if federated is not None:
        write(
            root / ".claude-plugin" / "federated-marketplaces.json",
            json.dumps({"marketplaces": federated}),
        )
    return root


def test_a_commit_that_is_an_option_is_refused_and_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    pin = {"source": "git", "url": "https://example.invalid/r.git"}
    pin["commit"] = f"--upload-pack=touch {marker};"
    market = _claude_market(tmp_path / "market", [{"name": "p", "source": pin}])
    summary = PluginStore(tmp_path / "store").install(str(market))
    assert not marker.exists()
    assert [d["code"] for d in summary["diagnostics"]] == ["plugin_source_unavailable"]
    assert "not a hexadecimal commit id" in summary["diagnostics"][0]["message"]


@pytest.mark.parametrize("ref", ["--upload-pack=touch x", "a..b", "bad ref"])
def test_a_ref_that_is_not_a_branch_or_tag_name_is_refused(tmp_path: Path, ref: str) -> None:
    with pytest.raises(SourceError, match="not a branch or tag name"):
        fetch("https://example.invalid/r.git", cache=tmp_path, ref=ref)


@pytest.mark.parametrize(
    "url", ["/", "file:///etc", "ext::sh -c touch% x", "--upload-pack=x", "../elsewhere"]
)
def test_a_declared_source_must_be_a_network_url(tmp_path: Path, url: str) -> None:
    with pytest.raises(SourceError, match="https, ssh or git URL"):
        fetch(url, cache=tmp_path, commit="a" * 40, declared=True)


def test_a_federated_marketplace_cannot_name_a_folder_on_this_machine(tmp_path: Path) -> None:
    local = plugin(tmp_path / "secrets", "loot")
    fed = {"name": "child", "source": {"source": "git", "url": str(local), "commit": "a" * 40}}
    market = _claude_market(tmp_path / "market", [], federated=[fed])
    store = PluginStore(tmp_path / "store")
    with pytest.raises(store_module.StoreError, match="https, ssh or git URL"):
        store.install(str(market))
    assert "child" not in store.marketplaces()


def test_a_git_plugin_path_cannot_climb_out_of_its_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    plugin(tmp_path, "outside")

    def fake_fetch(url: str, *, declared: bool = False, **kwargs: object) -> FetchedSource:
        if not declared:
            return fetch(url, **kwargs)  # the marketplace folder itself
        return FetchedSource(path=checkout, url=url, commit="a" * 40, local=False)

    monkeypatch.setattr(store_module, "fetch", fake_fetch)
    pin = {"source": "git-subdir", "url": "https://x/y.git", "path": "../outside"}
    pin["commit"] = "a" * 40
    market = _claude_market(tmp_path / "market", [{"name": "p", "source": pin}])
    summary = PluginStore(tmp_path / "store").install(str(market))
    assert [d["code"] for d in summary["diagnostics"]] == ["plugin_source_missing"]


def test_a_folder_the_person_names_still_installs(tmp_path: Path) -> None:
    folder = plugin(tmp_path, "mine")
    fetched = sources.fetch(str(folder), cache=tmp_path / "cache")
    assert fetched.local and fetched.path == folder.resolve()
