"""A marketplace file's git sources never reach ``git`` as options or local folders."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from clio_agent.platform_paths import TreeRemovalError, remove_tree
from clio_agent.plugins import sources
from clio_agent.plugins import store as store_module
from clio_agent.plugins.sources import FetchedSource, SourceError, fetch
from clio_agent.plugins.store import PluginStore, StoreError
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
    summary = store.install(str(market))
    assert [d["code"] for d in summary["diagnostics"]] == ["federated_source_unavailable"]
    assert "https, ssh or git URL" in summary["diagnostics"][0]["message"]
    assert "child" not in store.marketplaces() and "m" in store.marketplaces()


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


# ---------------------------------------------------- remove, then add again


def _git_market(root: Path) -> tuple[Path, str]:
    """A local git repository holding an empty marketplace; returns it and its commit."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    _claude_market(root, [])
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-q", "-m", "m"]):
        subprocess.run([git, *args], cwd=root, check=True, capture_output=True)
    head = subprocess.run(
        [git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    )
    return root, head.stdout.strip()


def _read_only_tree(folder: Path) -> None:
    """A folder as git leaves its objects: read-only files Windows refuses to delete."""
    for relative in ("objects/pack/a.pack", "objects/ab/cdef"):
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        path.chmod(stat.S_IREAD)


def _cache_folders(store: PluginStore) -> list[str]:
    cache = store.root / "sources"
    return sorted(p.name for p in cache.iterdir()) if cache.is_dir() else []


def test_remove_deletes_the_fetched_checkout_and_adding_again_works(tmp_path: Path) -> None:
    repo, commit = _git_market(tmp_path / "repo")
    store = PluginStore(tmp_path / "store")
    store.install(str(repo), commit=commit, name="m")
    key = sources.cache_key(str(repo.resolve()), commit)
    assert _cache_folders(store) == [key]
    assert store.marketplaces()["m"]["fetch_cache"] == [key]
    _read_only_tree(store.root / "sources" / key)

    assert store.uninstall("m") == ["m"]
    assert "m" not in store.marketplaces()
    assert _cache_folders(store) == []

    store.install(str(repo), commit=commit, name="m")
    assert "m" in store.marketplaces() and _cache_folders(store) == [key]


def test_a_stale_read_only_staging_or_checkout_never_blocks_a_fetch(tmp_path: Path) -> None:
    repo, commit = _git_market(tmp_path / "repo")
    cache = tmp_path / "cache"
    key = sources.cache_key(str(repo.resolve()), commit)
    # What an earlier release's crashed fetch left: its fixed staging name, and a
    # checkout half deleted by an rmtree that ignored the read-only files.
    _read_only_tree(cache / f".{key}.partial")
    _read_only_tree(cache / key)
    fetched = fetch(str(repo), cache=cache, commit=commit)
    assert fetched.path == cache / key and fetched.commit == commit
    assert (cache / key / ".git").exists() and not (cache / key / "objects").exists()
    assert sorted(p.name for p in cache.iterdir()) == [key]


def test_a_failed_fetch_leaves_no_staging_behind(tmp_path: Path) -> None:
    repo, _commit = _git_market(tmp_path / "repo")
    cache = tmp_path / "cache"
    with pytest.raises(SourceError):
        fetch(str(repo), cache=cache, commit="0" * 40)
    assert list(cache.iterdir()) == []


def test_a_checkout_that_cannot_be_deleted_is_reported_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, commit = _git_market(tmp_path / "repo")
    store = PluginStore(tmp_path / "store")
    store.install(str(repo), commit=commit, name="m")

    def held(cache: Path, name: str) -> None:
        raise TreeRemovalError(f"cannot delete {cache / name}: held open")

    monkeypatch.setattr(store_module, "remove_cached", held)
    with pytest.raises(StoreError, match="could not be deleted") as caught:
        store.uninstall("m")
    assert caught.value.code == "marketplace_files_not_removed"
    assert "m" not in store.marketplaces()


def test_remove_takes_the_federated_marketplaces_and_spares_shared_checkouts(
    tmp_path: Path,
) -> None:
    repo, commit = _git_market(tmp_path / "repo")
    store = PluginStore(tmp_path / "store")
    store.install(str(repo), commit=commit, name="parent")
    store.install(str(repo), commit=commit, name="child")
    store.install(str(repo), commit=commit, name="other")
    state = store.read_state()
    state["marketplaces"]["child"]["parent"] = "parent"
    del state["marketplaces"]["other"]["fetch_cache"]  # installed before it was recorded
    store._write_state(state)

    assert store.uninstall("parent") == ["parent", "child"]
    assert sorted(store.marketplaces()) == ["other"]
    # ``other`` still uses the same checkout, so it stays.
    assert _cache_folders(store) == [sources.cache_key(str(repo.resolve()), commit)]
    store.uninstall("other")
    assert _cache_folders(store) == []


def test_remove_tree_deletes_read_only_files_and_tolerates_a_missing_folder(
    tmp_path: Path,
) -> None:
    _read_only_tree(tmp_path / "tree")
    remove_tree(tmp_path / "tree")
    assert not (tmp_path / "tree").exists()
    remove_tree(tmp_path / "tree")
