"""A daemon this home spawns is attached to only once it holds its port (FN-14).

Two isolated stacks on one machine both found the default port free and spawned at
once. The loser's ``clio_run`` exited on the bound port ("Failed to initialize Clio
runtime"), but its wait returned as soon as *anything* answered on the port: the
winner's daemon. The loser attached to the other home's store, wrote its
conversations there, and after a restart (when ownership was checked and its own
daemon moved to a private port) every conversation answered 404 and the session
record migrations ran again on its own, empty store.

``runtime_ownership.confirm_own_daemon_on_port`` closes that: after the wait the listener
must be this home's pidfile daemon, else the home moves off the port and spawns again.

Hermetic: a private state directory, ports only written into configs (the listening
set is faked), and spawns that only record what they were asked.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from clio_agent.arc import (
    clio_core_config,
    crashed_clients,
    runtime_ownership,
    runtime_stop,
    storage,
)
from clio_agent.arc.runtime_crash import DaemonSpawnFailed
from clio_agent.arc.runtime_ownership import SPAWN_LOST_PORT, ClioCoreForeignDaemonError

# The default port both homes race for; only ever written into configs.
_RACED_PORT = 29413


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A private runtime state dir (lock, pidfile, client registry) for this test only."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(state_dir))
    monkeypatch.setattr(storage, "_client_registered", False)
    monkeypatch.setattr(runtime_stop, "_runtime_shutdown_requested", False)
    crashed_clients.reset_daemon_replacement()
    yield state_dir
    storage._deregister_client()
    crashed_clients.reset_daemon_replacement()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait(timeout=30)
    return proc.pid


def _write_pidfile(pid: int, ctime: float = 0.0) -> None:
    storage._daemon_pidfile().write_text(f"{pid} {ctime!r}", encoding="utf-8")


def _generated_config(cte_dir: Path, port: int) -> Path:
    cte_dir.mkdir(parents=True, exist_ok=True)
    config = cte_dir / "cte.yaml"
    config.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=port,
            core_threads=4,
            conf_dir=(cte_dir / "conf").as_posix(),
            ram_budget="1GB",
            file_tier=(cte_dir / "storage.bin").as_posix(),
            file_capacity="8GB",
            metadata_log=(cte_dir / "metadata.log").as_posix(),
            indexer="",
        ),
        encoding="utf-8",
    )
    return config


class _Race:
    """The machine as the racing home sees it: who listens, and what it spawned."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, other_wins: int) -> None:
        self.listening: set[int] = set()
        self.spawn_ports: list[int] = []
        self.waited: list[int] = []
        self._other_wins = other_wins  # spawns whose port the other home takes first
        self._dead = _dead_pid()
        monkeypatch.setattr(storage, "_runtime_alive", lambda port: port in self.listening)
        monkeypatch.setattr(storage, "_spawn_runtime_daemon", self._spawn)
        monkeypatch.setattr(storage, "_wait_for_daemon_start", self._wait)
        monkeypatch.setattr(
            storage.core_control_port, "hold_control_port", lambda _port: None, raising=False
        )

    def _spawn(self, _core: object, cfg: str, _level: str) -> None:
        port = storage._resolve_runtime_port(cfg)
        self.spawn_ports.append(port)
        if len(self.spawn_ports) <= self._other_wins:
            # The other home binds first; ours exits on the bound port (pidfile: dead).
            self.listening.add(port)
            _write_pidfile(self._dead)
        else:
            # Ours binds: the pidfile names a live process that holds the listener.
            _write_pidfile(os.getpid(), psutil.Process(os.getpid()).create_time())
            self.listening.add(port)

    def _wait(self, port: int, _state: Path) -> None:
        self.waited.append(port)


def _own_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pidfile process (this test) holds the listener of a port ours bound."""
    monkeypatch.setattr(runtime_ownership, "listener_belongs_to", lambda _pid, _port: True)


def test_a_lost_spawn_race_moves_off_the_port_instead_of_attaching_there(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """FN-14: the other home's daemon answers the wait; this home never adopts it.

    **Sabotage:** drop ``confirm_own_daemon_on_port`` from ``_ensure_runtime_daemon`` (wait
    only) -> one spawn, the config keeps the raced port and the store attached is the
    other home's.
    """
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _RACED_PORT)
    chosen = runtime_ownership.private_port_candidates(state)[1]
    monkeypatch.setattr(runtime_ownership, "_block_free", lambda base: base == chosen)
    _own_listener(monkeypatch)
    race = _Race(monkeypatch, other_wins=1)
    monkeypatch.setattr(
        storage.daemon_version,
        "resolve_effective_config",
        lambda *_a, **_k: pytest.fail("adopted another home's daemon"),
    )

    with caplog.at_level("WARNING"):
        assert storage._ensure_runtime_daemon(object(), str(config), "error") == str(config)

    assert race.spawn_ports == [_RACED_PORT, chosen]
    assert race.waited == [_RACED_PORT, chosen]
    assert storage._resolve_runtime_port(str(config)) == chosen  # holds across restarts
    assert storage._live_client_pids() == [os.getpid()]
    assert SPAWN_LOST_PORT in caplog.text
    assert runtime_ownership.PORT_MOVED in caplog.text


def test_a_crash_seen_before_the_other_daemon_binds_is_the_same_lost_race(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait read our crash record first; the other home's daemon listens by now."""
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _RACED_PORT)
    chosen = runtime_ownership.private_port_candidates(state)[0]
    monkeypatch.setattr(runtime_ownership, "_block_free", lambda base: base == chosen)
    _own_listener(monkeypatch)
    race = _Race(monkeypatch, other_wins=1)

    def wait(port: int, _state: Path) -> None:
        race.waited.append(port)
        if port == _RACED_PORT:
            raise DaemonSpawnFailed("exited before binding port: Failed to initialize")

    monkeypatch.setattr(storage, "_wait_for_daemon_start", wait)

    assert storage._ensure_runtime_daemon(object(), str(config), "error") == str(config)
    assert race.spawn_ports == [_RACED_PORT, chosen]


def test_a_crash_with_nothing_on_the_port_still_fails_typed(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a race: our daemon failed alone, so the spawn failure is reported as is."""
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _RACED_PORT)
    original = config.read_bytes()
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: False)
    monkeypatch.setattr(storage, "_spawn_runtime_daemon", lambda *_a: None)

    def wait(_port: int, _state: Path) -> None:
        raise DaemonSpawnFailed("exited before binding port")

    monkeypatch.setattr(storage, "_wait_for_daemon_start", wait)

    with pytest.raises(DaemonSpawnFailed):
        storage._ensure_runtime_daemon(object(), str(config), "error")
    assert config.read_bytes() == original
    assert storage._client_registered is False


def test_losing_the_port_twice_refuses_typed(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _RACED_PORT)
    chosen = runtime_ownership.private_port_candidates(state)[0]
    monkeypatch.setattr(runtime_ownership, "_block_free", lambda base: base == chosen)
    race = _Race(monkeypatch, other_wins=2)

    with pytest.raises(ClioCoreForeignDaemonError):
        storage._ensure_runtime_daemon(object(), str(config), "error")
    assert race.spawn_ports == [_RACED_PORT, chosen]  # one retry, never a third spawn
    assert storage._client_registered is False


def test_a_lost_race_with_a_hand_written_config_refuses_and_leaves_it(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    config = _generated_config(tmp_path / "hand", _RACED_PORT)
    original = config.read_bytes()
    race = _Race(monkeypatch, other_wins=1)

    with pytest.raises(ClioCoreForeignDaemonError):
        storage._ensure_runtime_daemon(object(), str(config), "error")
    assert race.spawn_ports == [_RACED_PORT]
    assert config.read_bytes() == original


def test_a_spawn_that_wins_its_port_attaches_without_moving(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """The ordinary start (and every later restart): one spawn, the config untouched."""
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _RACED_PORT)
    original = config.read_bytes()
    _own_listener(monkeypatch)
    monkeypatch.setattr(
        runtime_stop, "kill_daemon_pidfile", lambda: pytest.fail("retired its own daemon")
    )
    race = _Race(monkeypatch, other_wins=0)

    with caplog.at_level("WARNING"):
        assert storage._ensure_runtime_daemon(object(), str(config), "error") == str(config)
    assert race.spawn_ports == [_RACED_PORT]
    assert config.read_bytes() == original
    assert SPAWN_LOST_PORT not in caplog.text


def test_the_real_ownership_check_sees_a_dead_pidfile_daemon_as_foreign(state: Path) -> None:
    """The live shape, unfaked: our pidfile names an exited daemon, a socket listens."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = int(other.getsockname()[1])
        _write_pidfile(_dead_pid())
        assert runtime_ownership._lost_to_foreign(port, lambda p: p == port) is True
