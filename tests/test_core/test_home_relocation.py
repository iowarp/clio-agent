"""A moved CLIO home stops using its old location.

Covers the shared path mechanics (:mod:`clio_agent.path_relocation`), the plugin store's
own relocation, MCP environments built elsewhere, and the home roots' relocation at
server start (:mod:`clio_agent.home_relocation`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent import home_relocation, path_relocation
from clio_agent.plugins import store_relocation
from clio_agent.tools import mcp_environment_relocation as environments


@pytest.fixture(autouse=True)
def _fresh_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_relocation, "_records", [])
    monkeypatch.setattr(store_relocation, "_checked", set())
    monkeypatch.setattr(environments, "_swept", set())


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _venv(environment: Path, built_at: Path) -> Path:
    """A uv environment as built at ``built_at``: its activation script names it."""
    (environment / "bin").mkdir(parents=True)
    (environment / "bin" / "activate").write_text(f"export X=1\nVIRTUAL_ENV='{built_at}'\n")
    (environment / "bin" / "web-mcp").write_text(f"#!{built_at}/bin/python\n")
    return environment


# ---- path mechanics ---------------------------------------------------------------------


def test_relocate_text_rewrites_whole_paths_only() -> None:
    moves = [("/a/home", "/b/a-longer/home")]

    text = path_relocation.relocate_text(
        'x /a/home/c "/a/home" /a/homeX/z (/a/home/q) --cfg=/a/home/w', moves
    )

    assert text == (
        'x /b/a-longer/home/c "/b/a-longer/home" /a/homeX/z (/b/a-longer/home/q) '
        "--cfg=/b/a-longer/home/w"
    )


def test_relocated_lets_the_deepest_root_decide() -> None:
    moves = [("/d/cte", "/d/cte/hosts/h")]

    assert path_relocation.relocated("/d/cte/x", moves) == "/d/cte/hosts/h/x"
    assert path_relocation.relocated("/d/cte/hosts/h/x", moves) == "/d/cte/hosts/h/x"


def test_relocate_text_handles_windows_paths() -> None:
    moves = [(r"C:\Users\a\AppData", r"D:\Profiles\b\AppData")]

    assert path_relocation.relocate_text(r"C:\Users\a\AppData\x\y", moves) == (
        r"D:\Profiles\b\AppData\x\y"
    )


def test_relocate_json_file_rewrites_nested_values_and_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"a": ["/old/x", {"/old/k": "/old/y"}], "n": 1, "o": "/other"}))

    assert path_relocation.relocate_json_file(path, [("/old", "/new")])

    assert json.loads(path.read_text()) == {
        "a": ["/new/x", {"/new/k": "/new/y"}],
        "n": 1,
        "o": "/other",
    }
    assert not path_relocation.relocate_json_file(path, [("/old", "/new")])  # nothing left


def test_a_venv_built_elsewhere_is_stale(tmp_path: Path) -> None:
    here = _venv(tmp_path / "here", tmp_path / "here")
    copied = _venv(tmp_path / "copied", tmp_path / "original")

    assert not path_relocation.venv_is_stale(here)
    assert path_relocation.venv_is_stale(copied)
    assert path_relocation.venv_recorded_path(copied) == str(tmp_path / "original")
    assert not path_relocation.venv_is_stale(tmp_path / "no-venv")


def test_a_windows_venv_names_itself_in_activate_bat(tmp_path: Path) -> None:
    environment = tmp_path / "env"
    (environment / "Scripts").mkdir(parents=True)
    (environment / "Scripts" / "activate.bat").write_text(
        '@for %%i in ("C:\\old\\env") do @set "VIRTUAL_ENV=%%~fi"\n'
    )

    assert path_relocation.venv_recorded_path(environment) == "C:\\old\\env"


# ---- the plugin store -------------------------------------------------------------------


def _plugin_store(root: Path) -> None:
    snapshot = root / "snapshots" / "clio" / "f13d1acffb2d0c1e"
    plugin = snapshot / "plugins" / "web"
    state = {
        "version": 1,
        "marketplaces": {"clio": {"snapshot": str(snapshot), "url": "/install/marketplace"}},
        "plugins": {
            "clio/web": {
                "root": str(plugin),
                "skills": [{"id": "s", "description": "", "path": str(plugin / "skills/s.md")}],
                "mcp_servers": {"web": {"command": "clio-kit", "args": ["--cfg", f"{plugin}/c"]}},
            }
        },
        "pending": {},
        "agents": {},
    }
    (root / "resolved").mkdir(parents=True)
    (root / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True))
    (root / "resolved" / "d1.json").write_text(json.dumps({"plugins": [{"root": str(plugin)}]}))


def test_a_moved_plugin_store_points_at_its_own_snapshots(tmp_path: Path) -> None:
    old, new = tmp_path / "old" / "plugins", tmp_path / "a-longer-new" / "plugins"
    _plugin_store(old)
    new.parent.mkdir()
    old.rename(new)
    _plugin_store(old)  # a decoy at the old path
    decoy = _tree(old)

    from filelock import FileLock

    entry = store_relocation.relocate_plugin_store(new, FileLock(str(new / "state.lock")))

    assert entry is not None and entry["from"] == str(old) and entry["to"] == str(new)
    assert entry["files"] == 2
    for path in (new / "state.json", new / "resolved" / "d1.json"):
        assert str(old) not in path.read_text()
    state = json.loads((new / "state.json").read_text())
    plugin = state["plugins"]["clio/web"]
    assert plugin["root"] == str(new / "snapshots/clio/f13d1acffb2d0c1e/plugins/web")
    assert plugin["mcp_servers"]["web"]["args"][1].startswith(str(new))
    assert state["marketplaces"]["clio"]["url"] == "/install/marketplace"
    assert _tree(old) == decoy
    assert path_relocation.recorded() == [entry]


def test_a_plugin_store_in_place_is_untouched(tmp_path: Path) -> None:
    from filelock import FileLock

    root = tmp_path / "plugins"
    _plugin_store(root)
    before = _tree(root)

    assert store_relocation.relocate_plugin_store(root, FileLock(str(root / "l"))) is None
    assert {k: v for k, v in _tree(root).items() if k != "l"} == before


# ---- MCP environments -------------------------------------------------------------------


def test_environments_built_elsewhere_are_removed_for_a_rebuild(tmp_path: Path) -> None:
    cache = tmp_path / "new" / "mcp-runtime"
    root = cache / "mcp-environments"
    stale = _venv(root / "web-e0c5", tmp_path / "old" / "mcp-runtime/mcp-environments/web-e0c5")
    fresh = _venv(root / "pandas-66677", root / "pandas-66677")

    removed = environments.remove_stale_environments(cache)

    assert removed == ["web-e0c5"]
    assert not stale.exists() and fresh.is_dir()
    (entry,) = path_relocation.recorded()
    assert entry["reason"] == environments.MCP_ENVIRONMENT_RELOCATED
    assert environments.remove_stale_environments(cache) == []  # once per process


def test_an_environment_a_live_launcher_holds_is_left(tmp_path: Path) -> None:
    from clio_kit.environment_locks import EnvironmentInUseMarker

    cache = tmp_path / "mcp-runtime"
    stale = _venv(cache / "mcp-environments" / "web-e0c5", tmp_path / "old")

    with EnvironmentInUseMarker(cache, "web-e0c5"):
        assert environments.remove_stale_environments(cache) == []

    assert stale.is_dir()


def test_a_learned_launch_must_target_this_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    here = _venv(cache / "mcp-environments" / "web-1", cache / "mcp-environments" / "web-1")
    there = _venv(tmp_path / "old" / "mcp-environments" / "web-1", tmp_path / "old/x")
    for environment in (here, there):
        (environment / "bin" / "python").write_text("")

    assert environments.plan_targets_this_cache(
        [str(here / "bin/python"), str(here / "bin/web-mcp")], cache
    )
    assert not environments.plan_targets_this_cache(
        [str(there / "bin/python"), str(there / "bin/web-mcp")], cache
    )


# ---- the home roots ---------------------------------------------------------------------


def _home(home: Path) -> dict[str, Path]:
    roots = {role: home / role for role in ("config", "data", "state", "cache")}
    for root in roots.values():
        root.mkdir(parents=True)
    service = roots["data"] / "services" / "vm" / "searxng"
    service.mkdir(parents=True)
    (service / "owner.json").write_text(
        json.dumps({"owner": "o", "host": "vm", "root": str(service)})
    )
    _venv(service / "environment" / ".venv", service / "environment" / ".venv")
    (roots["state"] / "server").mkdir()
    (roots["state"] / "server" / "infrastructure.json").write_text(
        json.dumps({"services": {"vm/searxng": {"storage.service_directory": str(service)}}})
    )
    (roots["config"] / "workspaces.json").write_text(
        json.dumps({"workspaces": [{"root_path": "/elsewhere/project"}]})
    )
    return roots


def _use(monkeypatch: pytest.MonkeyPatch, roots: dict[str, Path]) -> None:
    monkeypatch.setattr(home_relocation, "_roots", lambda: roots)
    monkeypatch.setenv("CLIO_KIT_CACHE_DIR", str(roots["cache"] / "mcp-runtime"))


def test_a_moved_home_is_relocated_from_its_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = tmp_path / "old" / "home", tmp_path / "a-longer-new-place" / "home"
    _home(old)  # a home from before the markers: its records say where it was
    new.parent.mkdir()
    old.rename(new)
    roots = {role: new / role for role in ("config", "data", "state", "cache")}
    _use(monkeypatch, roots)

    entries = home_relocation.relocate_home()

    assert [(e["role"], e["from"], e["to"]) for e in entries] == [
        ("data", str(old / "data"), str(roots["data"]))
    ]
    service = roots["data"] / "services" / "vm" / "searxng"
    assert json.loads((service / "owner.json").read_text())["root"] == str(service)
    infrastructure = (roots["state"] / "server" / "infrastructure.json").read_text()
    assert str(service) in infrastructure and str(old) not in infrastructure
    assert not (service / "environment" / ".venv").exists()  # rebuilt by a reinstall
    reasons = [entry["reason"] for entry in path_relocation.recorded()]
    assert reasons == [
        home_relocation.NATIVE_SERVICE_RELOCATED,
        home_relocation.CLIO_HOME_RELOCATED,
    ]
    for root in roots.values():
        assert json.loads((root / home_relocation.MARKER).read_text())["path"] == str(root)
    assert home_relocation.relocate_home() == []  # done: the next start has nothing to do


def test_a_marker_naming_another_place_relocates_that_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _home(tmp_path / "home")
    _use(monkeypatch, roots)
    home_relocation.relocate_home()  # writes the markers
    moved = tmp_path / "moved" / "home"
    moved.parent.mkdir()
    (tmp_path / "home").rename(moved)
    roots = {role: moved / role for role in roots}
    _use(monkeypatch, roots)
    environment = _venv(
        roots["cache"] / "mcp-runtime" / "mcp-environments" / "web-1",
        tmp_path / "home" / "cache" / "mcp-runtime" / "mcp-environments" / "web-1",
    )

    entries = home_relocation.relocate_home()

    assert {entry["role"] for entry in entries} == {"config", "data", "state", "cache"}
    assert not environment.exists()
    workspaces = json.loads((roots["config"] / "workspaces.json").read_text())
    assert workspaces["workspaces"][0]["root_path"] == "/elsewhere/project"  # not CLIO's


def test_a_home_in_place_is_left_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _home(tmp_path / "home")
    _use(monkeypatch, roots)
    home_relocation.relocate_home()
    before = _tree(tmp_path / "home")

    assert home_relocation.relocate_home() == []
    assert _tree(tmp_path / "home") == before
