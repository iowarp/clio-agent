"""Contract resolution and real write-footprint regressions."""

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from clio_agent import paths
from clio_agent.path_migration import Move, migrate


@pytest.mark.parametrize(
    "platform,home,expected",
    [
        (
            "linux",
            "/home/dev",
            {
                "config": "/home/dev/.config/clio-agent",
                "data": "/home/dev/.local/share/clio-agent",
                "state": "/home/dev/.local/state/clio-agent",
                "cache": "/home/dev/.cache/clio-agent",
            },
        ),
        (
            "macos",
            "/Users/dev",
            {
                "config": "/Users/dev/Library/Application Support/clio-agent/config",
                "data": "/Users/dev/Library/Application Support/clio-agent/data",
                "state": "/Users/dev/Library/Application Support/clio-agent/state",
                "cache": "/Users/dev/Library/Caches/clio-agent",
            },
        ),
        (
            "windows",
            r"C:\Users\dev",
            {
                "config": r"C:\Users\dev\AppData\Roaming\clio-agent\config",
                "data": r"C:\Users\dev\AppData\Local\clio-agent\data",
                "state": r"C:\Users\dev\AppData\Local\clio-agent\state",
                "cache": r"C:\Users\dev\AppData\Local\clio-agent\cache",
            },
        ),
    ],
)
def test_native_roles_are_distinct(
    platform: paths.Platform, home: str, expected: dict[paths.Role, str]
) -> None:
    roots = [paths.resolve_root(role, home=home, env={}, platform=platform) for role in expected]
    assert [str(p) for p in roots] == list(expected.values())
    assert len(set(roots)) == 4
    assert paths.resolve_root("runtime", home=home, env={}, platform=platform) is None


def test_precedence_and_legacy_layout() -> None:
    env = {
        "CLIO_AGENT_HOME": "/agent",
        "CLIO_USER_DIR": "/legacy",
        "CLIO_AGENT_CONFIG_DIR": "/config",
    }

    def resolve(role: paths.Role) -> PurePosixPath | PureWindowsPath | None:
        return paths.resolve_root(role, home="/home/u", env=env, platform="linux")

    assert resolve("config") == PurePosixPath("/config")
    assert resolve("data") == PurePosixPath("/agent/data")
    assert resolve("runtime") is None
    del env["CLIO_AGENT_HOME"]
    del env["CLIO_AGENT_CONFIG_DIR"]
    assert resolve("config") == PurePosixPath("/legacy")
    assert resolve("data") == PurePosixPath("/legacy/data")


@pytest.mark.parametrize("name", ["CLIO_AGENT_HOME", "CLIO_AGENT_STATE_DIR", "CLIO_USER_DIR"])
def test_relative_overrides_fail(name: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        paths.resolve_root("state", home="/home/u", env={name: "relative"}, platform="linux")


def test_invalid_xdg_and_empty_override_fall_back(caplog: pytest.LogCaptureFixture) -> None:
    root = paths.resolve_root(
        "state",
        home="/home/u",
        env={"XDG_STATE_HOME": " relative", "CLIO_AGENT_HOME": "  "},
        platform="linux",
    )
    assert root == PurePosixPath("/home/u/.local/state/clio-agent")
    assert "native fallback" in caplog.text


def test_windows_ignores_xdg() -> None:
    root = paths.resolve_root(
        "config",
        home=r"C:\Users\u",
        env={"XDG_CONFIG_HOME": "/wrong", "APPDATA": r"D:\Roaming"},
        platform="windows",
    )
    assert root == PureWindowsPath(r"D:\Roaming\clio-agent\config")


def test_relative_legacy_xdg_cannot_redirect_storage_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing relative legacy directory must not defeat native fallback."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relative" / "clio-agent").mkdir(parents=True)
    home = tmp_path / "home"
    env = {"XDG_CONFIG_HOME": "relative"}
    root = paths.user_config_dir_for(home, env)
    expected = paths.resolve_root("config", home=home, env=env, platform=paths._platform())
    assert root == expected
    assert root.is_absolute()


def test_workspace_storage_and_server_state_do_not_follow_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    server = paths.server_state_dir()
    workspace = paths.workspace_state_dir(first)
    monkeypatch.chdir(second)
    assert paths.server_state_dir() == server
    assert paths.workspace_state_dir(first) == workspace
    assert paths.workspace_state_dir(second) != workspace
    assert list(first.iterdir()) == []
    assert list(second.iterdir()) == []


def test_explicit_workspace_init_preserves_ignore(tmp_path: Path) -> None:
    project = paths.initialize_workspace(tmp_path)
    assert (project / "local" / ".gitignore").read_bytes() == b"*\n"
    paths.initialize_workspace(tmp_path)
    (project / "local" / ".gitignore").write_text("keep\n")
    with pytest.raises(ValueError, match="exactly"):
        paths.initialize_workspace(tmp_path)
    assert (project / "local" / ".gitignore").read_text() == "keep\n"


def test_migration_preview_and_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setattr("clio_agent.path_migration._assert_idle", lambda: None)
    source = tmp_path / "legacy" / "sessions.json"
    source.parent.mkdir()
    source.write_bytes(b'{"sessions":[]}')
    destination = paths.server_state_dir() / "sessions.json"
    moves = [Move(source, destination)]
    report = migrate(moves)
    assert not report["applied"] and source.exists() and not destination.exists()
    assert report["moves"][0]["bytes"] == 15
    report = migrate(moves, apply=True)
    assert report["applied"] and not source.exists()
    assert destination.read_bytes() == b'{"sessions":[]}'
    assert (Path(report["backups"]) / "backup-0").read_bytes() == destination.read_bytes()
    assert (Path(report["backups"]) / "receipt.json").is_file()


def test_migration_refuses_occupied_destination_and_live_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"valuable")
    target = tmp_path / "target"
    target.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        migrate([Move(source, target)], apply=True)
    assert source.read_bytes() == b"valuable" and target.read_bytes() == b"other"
    target.unlink()

    def busy() -> None:
        raise RuntimeError("live PIDs")

    monkeypatch.setattr("clio_agent.path_migration._assert_idle", busy)
    with pytest.raises(RuntimeError, match="live PIDs"):
        migrate([Move(source, target)], apply=True)
    assert source.exists() and not target.exists()


def test_failed_second_copy_rolls_back_first_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clio_agent.path_migration as migration

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setattr(migration, "_assert_idle", lambda: None)
    sources = [tmp_path / "first", tmp_path / "second"]
    for source in sources:
        source.write_bytes(b"original")
    moves = [Move(source, tmp_path / "new" / source.name) for source in sources]
    copy = migration._copy

    def fail_copy(source: Path, target: Path) -> None:
        if target == moves[1].destination:
            raise OSError("disk full")
        copy(source, target)

    monkeypatch.setattr(migration, "_copy", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        migrate(moves, apply=True)
    assert all(source.read_bytes() == b"original" for source in sources)
    assert all(not move.destination.exists() for move in moves)


def test_migration_refuses_overlapping_and_linked_trees(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"keep")
    with pytest.raises(ValueError, match="overlap"):
        migrate([Move(source, source / "nested")])


def test_namespaced_config_is_redacted_but_user_inputs_are_not() -> None:
    from clio_agent.gact.routes.workspace_file_policy import workspace_read_redaction_reason

    assert (
        workspace_read_redaction_reason(Path(".clio-agent/shared/config.yaml"))
        == "workspace_secret_config"
    )
    assert workspace_read_redaction_reason(Path("inputs/config.yaml")) is None


def test_migration_preserves_legacy_arc_until_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.path_migration import plan

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setattr("clio_agent.path_migration._assert_idle", lambda: None)
    server = tmp_path / "old-install"
    legacy = server / ".clio/agent/arc"
    legacy.mkdir(parents=True)
    (legacy / "record.msgpack").write_bytes(b"keep-history")
    monkeypatch.chdir(server)
    assert paths.arc_data_dir() == legacy
    migrate(plan(server=server), apply=True)
    assert paths.arc_data_dir() == paths.user_data_dir() / "arc"
    assert (paths.arc_data_dir() / "record.msgpack").read_bytes() == b"keep-history"


def test_home_migration_does_not_claim_core_owned_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.path_migration import plan

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    core = tmp_path / ".clio"
    core.mkdir()
    (core / "clio.yaml").write_text("core-owned", encoding="utf-8")
    (core / "plans").mkdir()
    moves = plan(home=tmp_path)
    assert [move.source for move in moves] == [core / "plans"]
    assert (core / "clio.yaml").read_text(encoding="utf-8") == "core-owned"
