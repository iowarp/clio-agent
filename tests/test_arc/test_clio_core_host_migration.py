"""One-time move of pre-host-key clio-core state into this host's directories."""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

import pytest

from clio_agent import conf, paths
from clio_agent.arc import clio_core_config
from clio_agent.arc.clio_core_host_migration import (
    CTE_STORE_MIGRATED,
    CTE_STORE_MIGRATION_FAILED,
    CTE_STORE_MIGRATION_SKIPPED_IN_USE,
    CTE_STORE_MIGRATION_SKIPPED_SHARED,
    RUNTIME_STATE_MIGRATED,
    RUNTIME_STATE_MIGRATION_SKIPPED_IN_USE,
    RUNTIME_STATE_MIGRATION_SKIPPED_SHARED,
    migrate_legacy_cte_store,
)

DEAD_PID = 1_999_999_999


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A home directory on this host (``desk``) with no state-dir override."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLIO_RUNTIME_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(paths, "user_data_dir", lambda: home / "data")
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=home, cwd=tmp_path / "cwd", env={}))
    monkeypatch.setattr(clio_core_config.socket, "gethostname", lambda: "desk")
    return home


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _legacy_store(home: Path, *, port: int | None = None) -> Path:
    """An old-layout store in <data>/cte, config pointing at its own files."""

    legacy = home / "data" / "cte"
    (legacy / "conf").mkdir(parents=True)
    (legacy / "conf" / "runtime.conf").write_bytes(b"conf")
    (legacy / "storage.bin_node0").write_bytes(b"\x00" * 4096)
    (legacy / "metadata.log").write_bytes(b"log")
    (legacy / "cte.yaml").write_bytes(
        (
            f"networking:\n  port: {port or _free_port()}\n"
            f'runtime:\n  conf_dir: "{(legacy / "conf").as_posix()}"\n'
            f'storage:\n  - path: "{(legacy / "storage.bin").as_posix()}"\n'
            f'metadata_log_path: "{(legacy / "metadata.log").as_posix()}"\n'
            "capacity_limit: 50GB\n"
        ).encode()
    )
    return legacy


def test_a_fresh_upgrade_moves_the_old_store_into_this_hosts_dir(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _legacy_store(home)
    with caplog.at_level(logging.WARNING):
        cfg = Path(clio_core_config.default_cte_config_path())

    host_dir = legacy / "hosts" / "desk"
    assert cfg == host_dir / "cte.yaml"
    assert sorted(entry.name for entry in legacy.iterdir()) == ["hosts"]
    assert (host_dir / "storage.bin_node0").read_bytes() == b"\x00" * 4096
    assert (host_dir / "conf" / "runtime.conf").read_bytes() == b"conf"
    text = cfg.read_text(encoding="utf-8")
    # The config keeps its values and names the moved files.
    assert (host_dir / "storage.bin").as_posix() in text
    assert (host_dir / "conf").as_posix() in text
    assert (host_dir / "metadata.log").as_posix() in text
    assert "capacity_limit: 50GB" in text
    assert f"{legacy.as_posix()}/storage.bin" not in text
    assert f"reason={CTE_STORE_MIGRATED}" in caplog.text


def test_an_already_migrated_store_is_left_alone(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _legacy_store(home)
    host_dir = legacy / "hosts" / "desk"
    host_dir.mkdir(parents=True)
    (host_dir / "cte.yaml").write_text("networking:\n  port: 9413\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cfg = Path(clio_core_config.default_cte_config_path())

    assert cfg.read_text(encoding="utf-8") == "networking:\n  port: 9413\n"
    assert (legacy / "storage.bin_node0").exists()
    assert "migrat" not in caplog.text


def test_a_store_a_running_daemon_may_use_is_not_moved(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        legacy = _legacy_store(home, port=listener.getsockname()[1])
        with caplog.at_level(logging.WARNING):
            result = migrate_legacy_cte_store(
                legacy, legacy / "hosts" / "desk", runtime_root=home / ".clio"
            )
    finally:
        listener.close()
    assert result == CTE_STORE_MIGRATION_SKIPPED_IN_USE
    assert (legacy / "storage.bin_node0").exists()
    assert not (legacy / "hosts" / "desk").exists()
    assert f"reason={CTE_STORE_MIGRATION_SKIPPED_IN_USE}" in caplog.text


def test_a_store_whose_recorded_daemon_is_alive_is_not_moved(home: Path) -> None:
    legacy = _legacy_store(home)
    runtime_root = home / ".clio"
    runtime_root.mkdir()
    (runtime_root / "clio-runtime.pid").write_text(f"{os.getpid()} ", encoding="utf-8")
    result = migrate_legacy_cte_store(legacy, legacy / "hosts" / "desk", runtime_root=runtime_root)
    assert result == CTE_STORE_MIGRATION_SKIPPED_IN_USE
    assert (legacy / "cte.yaml").exists()


def test_on_a_shared_home_with_another_hosts_store_nothing_moves(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _legacy_store(home)
    (legacy / "hosts" / "ares").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        cfg = Path(clio_core_config.default_cte_config_path())

    assert (legacy / "storage.bin_node0").exists() and (legacy / "cte.yaml").exists()
    # This host starts its own store beside the other host's.
    assert cfg == legacy / "hosts" / "desk" / "cte.yaml"
    assert "storage.bin_node0" not in [entry.name for entry in cfg.parent.iterdir()]
    assert f"reason={CTE_STORE_MIGRATION_SKIPPED_SHARED}" in caplog.text


def test_a_failed_move_puts_back_what_it_moved(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _legacy_store(home)
    before = sorted(entry.name for entry in legacy.iterdir())
    real_rename = Path.rename
    calls = {"n": 0}

    def flaky_rename(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk went away")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with caplog.at_level(logging.WARNING):
        result = migrate_legacy_cte_store(
            legacy, legacy / "hosts" / "desk", runtime_root=home / ".clio"
        )
    assert result == CTE_STORE_MIGRATION_FAILED
    assert sorted(entry.name for entry in legacy.iterdir() if entry.name != "hosts") == before
    assert not (legacy / "hosts" / "desk").exists()
    assert "disk went away" in caplog.text


def _legacy_runtime(home: Path, pid: int) -> Path:
    root = home / ".clio"
    (root / "clio-runtime.clients").mkdir(parents=True)
    (root / "clio-runtime.pid").write_text(f"{pid} ", encoding="utf-8")
    (root / "clio-runtime.log").write_text("old daemon log\n", encoding="utf-8")
    (root / "clio-runtime.lock").write_text("", encoding="utf-8")
    (root / "clio.yaml").write_text("clio-core's own config\n", encoding="utf-8")
    return root


def test_old_bookkeeping_moves_into_this_hosts_dir_once(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _legacy_runtime(home, DEAD_PID)
    with caplog.at_level(logging.WARNING):
        state = clio_core_config.runtime_state_dir()
        again = clio_core_config.runtime_state_dir()

    assert state == again == root / "hosts" / "desk"
    assert (state / "clio-runtime.log").read_text(encoding="utf-8") == "old daemon log\n"
    assert (state / "clio-runtime.clients").is_dir()
    assert not (root / "clio-runtime.pid").exists()
    # clio-core's own files are not CLIO's to move.
    assert (root / "clio.yaml").exists()
    assert caplog.text.count(f"reason={RUNTIME_STATE_MIGRATED}") == 1


def test_bookkeeping_of_a_live_daemon_stays(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = _legacy_runtime(home, os.getpid())
    with caplog.at_level(logging.WARNING):
        state = clio_core_config.runtime_state_dir()
    assert (root / "clio-runtime.pid").exists()
    assert not (state / "clio-runtime.pid").exists()
    assert f"reason={RUNTIME_STATE_MIGRATION_SKIPPED_IN_USE}" in caplog.text


def test_bookkeeping_on_a_shared_home_with_another_host_stays(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _legacy_runtime(home, DEAD_PID)
    (root / "hosts" / "ares").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        clio_core_config.runtime_state_dir()
    assert (root / "clio-runtime.log").exists()
    assert f"reason={RUNTIME_STATE_MIGRATION_SKIPPED_SHARED}" in caplog.text


def test_an_explicit_state_dir_is_never_migrated_into(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _legacy_runtime(home, DEAD_PID)
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path / "private"))
    assert clio_core_config.runtime_state_dir() == tmp_path / "private"
    assert (root / "clio-runtime.log").exists()
    assert list((tmp_path / "private").iterdir()) == []
