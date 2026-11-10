"""A clio-core daemon belongs to the home whose pidfile names it (``runtime_ownership``).

The daemon's port is machine-wide; its pidfile, client registry and spawn lock live per
home. A backend under one home found another home's daemon on the shared default port,
attached to it, and on shutdown (no live client in ITS registry) ran ``clio_run stop`` on
that port, killing the other home's daemon. These tests pin the rules that prevent it:
a listener this state directory's live pidfile daemon does not hold is foreign, is never
attached to, never stopped and never recycled; connect-or-spawn moves this home's
generated config to a free port block instead.

Binding-free and hermetic: the state directory is private to each test, every socket is
an ephemeral loopback port, and the stop helper and spawn are faked.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import psutil
import pytest

from clio_agent.arc import (
    clio_core_config,
    clio_core_daemon_version,
    clio_core_liveness,
    crashed_clients,
    runtime_ownership,
    runtime_stop,
    storage,
)
from clio_agent.arc.clio_core_daemon import DaemonMemorySnapshot, maybe_recycle_idle_daemon
from clio_agent.arc.runtime_ownership import ClioCoreForeignDaemonError

_GiB = 1024**3
# A port number only ever written into configs; nothing here binds it.
_FOREIGN_PORT = 29411


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


@pytest.fixture()
def listener() -> Any:
    """A real TCP listener held by this test process (an ephemeral loopback port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield sock
    sock.close()


@pytest.fixture()
def stand_in_daemon() -> Any:
    """A real child process that listens on nothing (this home's daemon, not bound)."""
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter, fixed argv
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _port(sock: socket.socket) -> int:
    return int(sock.getsockname()[1])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_pidfile(pid: int) -> None:
    ctime = psutil.Process(pid).create_time()
    storage._daemon_pidfile().write_text(f"{pid} {ctime!r}", encoding="utf-8")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait(timeout=30)
    return proc.pid


def _generated_config(cte_dir: Path, port: int) -> Path:
    """CLIO's generated ``cte.yaml``, rendered from the real default template."""
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


def _forbid_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither ``clio_run stop`` nor a pidfile kill may run."""

    def no_popen(*_a: object, **_k: object) -> None:
        raise AssertionError("clio_run stop ran against a daemon this home does not own")

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", no_popen)
    monkeypatch.setattr(
        storage, "_kill_daemon_pidfile", lambda: pytest.fail("killed a daemon not ours")
    )


# ---- classification ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pid", "listening", "belongs", "expected"),
    [
        (None, False, None, "none"),
        (None, True, None, "foreign"),
        (4242, False, None, "starting"),
        (4242, True, True, "owned"),
        (4242, True, None, "owned"),  # the OS will not say who listens: the pidfile decides
        (4242, True, False, "foreign"),
    ],
)
def test_daemon_ownership_classification(
    monkeypatch: pytest.MonkeyPatch,
    pid: int | None,
    listening: bool,
    belongs: bool | None,
    expected: str,
) -> None:
    def belongs_to(_pid: int, _port: int) -> bool | None:
        assert pid is not None, "no daemon of ours: nothing to look up"
        return belongs

    monkeypatch.setattr(runtime_ownership, "owned_daemon_pid", lambda: pid)
    monkeypatch.setattr(runtime_ownership, "listener_belongs_to", belongs_to)

    assert runtime_ownership.daemon_ownership(65001, listening=listening) == expected


def test_owned_daemon_pid_reads_only_a_live_pidfile_daemon(state: Path) -> None:
    assert runtime_ownership.owned_daemon_pid() is None  # no pidfile

    storage._daemon_pidfile().write_text(f"{_dead_pid()} 123.0", encoding="utf-8")
    assert runtime_ownership.owned_daemon_pid() is None  # dead

    me = os.getpid()
    storage._daemon_pidfile().write_text(f"{me} 1.0", encoding="utf-8")
    assert runtime_ownership.owned_daemon_pid() is None  # PID reused: create time differs

    _write_pidfile(me)
    assert runtime_ownership.owned_daemon_pid() == me


def test_listener_belongs_to_the_process_holding_the_socket(listener: socket.socket) -> None:
    assert runtime_ownership.listener_belongs_to(os.getpid(), _port(listener)) is True
    assert runtime_ownership.listener_belongs_to(os.getpid(), _free_port()) is False


def test_a_listener_a_descendant_holds_belongs_to_the_ancestor() -> None:
    """clio_run may bind through a child: the pidfile process's descendants count."""
    child = subprocess.Popen(  # noqa: S603 - fixed interpreter, fixed argv
        [
            sys.executable,
            "-c",
            "import socket, time\n"
            "s = socket.socket()\n"
            "s.bind(('127.0.0.1', 0))\n"
            "s.listen(1)\n"
            "print(s.getsockname()[1], flush=True)\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        port = int(child.stdout.readline())
        assert runtime_ownership.listener_belongs_to(os.getpid(), port) is True
        assert runtime_ownership.listener_belongs_to(child.pid, port) is True
    finally:
        child.kill()
        child.wait(timeout=10)


def test_listener_belongs_to_a_gone_process_is_false(listener: socket.socket) -> None:
    assert runtime_ownership.listener_belongs_to(_dead_pid(), _port(listener)) is False


def test_an_unreadable_listener_table_is_unknown_not_foreign(
    monkeypatch: pytest.MonkeyPatch, listener: socket.socket
) -> None:
    def denied(self: psutil.Process, kind: str = "inet") -> None:
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "net_connections", denied)

    assert runtime_ownership.listener_belongs_to(os.getpid(), _port(listener)) is None


# ---- stop guard and stop ------------------------------------------------------------


def test_stop_guard_without_a_pidfile_daemon_is_not_owned(
    state: Path, listener: socket.socket
) -> None:
    """Something listens, but no daemon of this home runs: never ours to stop."""
    assert runtime_ownership.stop_guard(_port(listener)) == "not_owned"


def test_stop_guard_owns_the_listener_its_pidfile_daemon_holds(
    state: Path, listener: socket.socket
) -> None:
    _write_pidfile(os.getpid())  # this process holds the listener; nothing is killed here

    assert runtime_ownership.stop_guard(_port(listener)) == "owned"


@pytest.mark.parametrize(("listening", "expected"), [(True, "foreign_listener"), (False, "owned")])
def test_stop_guard_when_its_daemon_does_not_hold_the_port(
    state: Path, monkeypatch: pytest.MonkeyPatch, listening: bool, expected: str
) -> None:
    """Ours runs but holds no listener: another holds the port, or ours has not bound."""
    _write_pidfile(os.getpid())  # stop_guard only classifies; it never kills
    monkeypatch.setattr(runtime_ownership, "listener_belongs_to", lambda _pid, _port: False)
    monkeypatch.setattr(clio_core_liveness, "_runtime_alive", lambda _port: listening)

    assert runtime_ownership.stop_guard(65001) == expected


def test_a_stop_with_no_daemon_of_ours_never_runs_clio_run_stop(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    listener: socket.socket,
) -> None:
    """The reported bug: another home's daemon on the port is left running.

    **Sabotage:** drop the ``stop_guard`` check in ``stop_runtime_daemon`` -> ``clio_run
    stop`` runs against the other home's daemon (``Popen`` fails the test).
    """
    port = _port(listener)  # the other home's daemon
    storage._daemon_pidfile().write_text(f"{_dead_pid()} 123.0", encoding="utf-8")  # stale
    _forbid_stop(monkeypatch)
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda _cfg: port)

    with caplog.at_level(logging.WARNING, logger=runtime_stop.logger.name):
        outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="not_owned")
    assert runtime_ownership.DAEMON_NOT_OWNED in caplog.text
    assert not storage._daemon_pidfile().exists()  # the stale record is dropped
    with socket.create_connection(("127.0.0.1", port), timeout=2.0):
        pass  # the other home's daemon still answers


def test_a_foreign_listener_stop_kills_only_this_homes_pidfile_daemon(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: socket.socket,
    stand_in_daemon: Any,
) -> None:
    """Ours runs but another home's daemon holds the port: ``clio_run stop`` (which reaches
    the port's daemon) never runs; only the daemon this home's pidfile names is killed."""
    port = _port(listener)  # held by another process than the pidfile daemon
    _write_pidfile(stand_in_daemon.pid)
    killed: list[bool] = []

    def no_popen(*_a: object, **_k: object) -> None:
        raise AssertionError("clio_run stop would stop the daemon holding the port")

    monkeypatch.setattr(runtime_stop.subprocess, "Popen", no_popen)
    monkeypatch.setattr(storage, "_kill_daemon_pidfile", lambda: killed.append(True))
    monkeypatch.setattr(runtime_stop, "_resolve_runtime_port", lambda _cfg: port)

    outcome = runtime_stop.stop_runtime_daemon("", "error")

    assert outcome == runtime_stop.StopOutcome(stopped=False, path="foreign_listener_kill")
    assert killed == [True]
    assert not storage._daemon_pidfile().exists()
    with socket.create_connection(("127.0.0.1", port), timeout=2.0):
        pass  # the port's own daemon is untouched


# ---- crashed clients and recycle ------------------------------------------------------


def test_crashed_clients_with_no_daemon_of_ours_prune_and_stop_nothing(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listener on the port with no live pidfile daemon belongs to another home.

    **Sabotage:** restore the port probe (``daemon_pid is None and not listening``) -> the
    other home's daemon is stopped.
    """
    dead = _dead_pid()
    registry = storage._client_registry_dir()
    registry.mkdir(parents=True, exist_ok=True)
    (registry / str(dead)).write_text("123.0", encoding="utf-8")
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)
    monkeypatch.setattr(
        runtime_stop, "stop_runtime_daemon", lambda *_a, **_k: pytest.fail("not ours")
    )

    assert crashed_clients.replace_daemon_left_by_crashed_clients("cfg.yaml", "error") is False

    assert not (registry / str(dead)).exists()  # pruned
    assert crashed_clients.daemon_replacement() == {}


def test_a_daemon_found_by_port_scan_is_never_recycled() -> None:
    """A critical, client-less daemon found only by its port is another home's."""
    snapshot = DaemonMemorySnapshot(
        pid=4321,
        pid_source="port_scan",
        rss_bytes=6 * _GiB,
        committed_bytes=12 * _GiB,
        thread_count=12,
        live_client_count=0,
        stale_client_count=0,
        registered_client_count=0,
        port=65001,
    )

    outcome = maybe_recycle_idle_daemon(
        enabled=True,
        snapshot=snapshot,
        stop_daemon=lambda: pytest.fail("recycled another home's daemon"),
        live_pids_fn=list,
        lock=contextlib.nullcontext(),
    )

    assert (outcome.recycled, outcome.reason, outcome.pid) == (False, "not_owned", 4321)


# ---- moving off a foreign daemon -------------------------------------------------------


def test_private_port_candidates_are_deterministic_and_distinct_per_state_dir() -> None:
    first = PurePosixPath("/home/alice/.clio/state")
    second = PurePosixPath("/home/bob/.clio/state")

    alice = runtime_ownership.private_port_candidates(first)  # type: ignore[arg-type]
    bob = runtime_ownership.private_port_candidates(second)  # type: ignore[arg-type]

    assert alice == runtime_ownership.private_port_candidates(first)  # type: ignore[arg-type]
    assert alice[0] != bob[0]
    assert sorted(alice) == sorted(bob)  # one shared slot table, entered at different slots
    assert len(set(alice)) == len(alice)
    for base in alice:
        assert 1024 <= base <= 65530
        assert base + runtime_ownership._PORT_BLOCK - 1 <= 65535
        assert (base - runtime_ownership._CANDIDATE_FIRST) % 8 == 0
    ordered = sorted(alice)
    assert all(b - a == 8 for a, b in zip(ordered, ordered[1:], strict=False))


def test_generated_config_is_only_clios_default_cte_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    generated = _generated_config(cte_dir, _FOREIGN_PORT)

    assert runtime_ownership.generated_config(str(generated)) is True
    assert runtime_ownership.generated_config(str(tmp_path / "hand" / "cte.yaml")) is False
    assert runtime_ownership.generated_config("") is False


def test_move_off_rewrites_only_the_networking_port(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _FOREIGN_PORT)
    before = config.read_text(encoding="utf-8").splitlines(keepends=True)
    candidates = runtime_ownership.private_port_candidates(state)
    chosen = candidates[3]
    probed: list[int] = []

    def block_free(base: int) -> bool:
        probed.append(base)
        return base == chosen

    monkeypatch.setattr(runtime_ownership, "_block_free", block_free)

    assert runtime_ownership.move_off_foreign_daemon(str(config), _FOREIGN_PORT, state) == chosen

    assert probed == candidates[:4]
    after = config.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(after) == len(before)
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert changed == [(f"  port: {_FOREIGN_PORT}\n", f"  port: {chosen}\n")]
    assert before[0] == "networking:\n"
    assert storage._resolve_runtime_port(str(config)) == chosen
    assert not config.with_name("cte.yaml.port-move").exists()


def test_move_off_refuses_a_hand_written_config_untouched(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    config = _generated_config(tmp_path / "hand", _FOREIGN_PORT)  # same shape, not CLIO's
    original = config.read_bytes()
    monkeypatch.setattr(
        runtime_ownership, "_block_free", lambda _base: pytest.fail("no port is probed")
    )

    with pytest.raises(ClioCoreForeignDaemonError) as info:
        runtime_ownership.move_off_foreign_daemon(str(config), _FOREIGN_PORT, state)

    assert info.value.error_type == runtime_ownership.FOREIGN_DAEMON_ON_PORT
    assert str(_FOREIGN_PORT) in str(info.value) and str(config) in str(info.value)
    assert config.read_bytes() == original


def test_move_off_with_no_free_block_refuses_typed(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _FOREIGN_PORT)
    original = config.read_bytes()
    monkeypatch.setattr(runtime_ownership, "_block_free", lambda _base: False)

    with pytest.raises(ClioCoreForeignDaemonError):
        runtime_ownership.move_off_foreign_daemon(str(config), _FOREIGN_PORT, state)

    assert config.read_bytes() == original


# ---- connect-or-spawn ------------------------------------------------------------------


def _wire_spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"spawned": [], "spawn_port": [], "waited": []}

    def spawn(_core: object, cfg: str, _level: str) -> None:
        seen["spawned"].append(cfg)
        seen["spawn_port"].append(storage._resolve_runtime_port(cfg))

    monkeypatch.setattr(storage, "_spawn_runtime_daemon", spawn)
    monkeypatch.setattr(
        storage, "_wait_for_daemon_start", lambda port, _state: seen["waited"].append(port)
    )
    return seen


def test_connect_or_spawn_never_adopts_another_homes_daemon(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug's attach half: the listener is not adopted, this home's generated
    config moves to a free block, and a daemon of its own spawns there.

    **Sabotage:** drop the ``daemon_ownership`` check in ``_ensure_runtime_daemon`` -> the
    foreign listener is adopted and nothing spawns.
    """
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _FOREIGN_PORT)
    chosen = runtime_ownership.private_port_candidates(state)[2]
    monkeypatch.setattr(runtime_ownership, "_block_free", lambda base: base == chosen)
    monkeypatch.setattr(storage, "_runtime_alive", lambda port: port == _FOREIGN_PORT)
    monkeypatch.setattr(
        storage.daemon_version,
        "resolve_effective_config",
        lambda *_a, **_k: pytest.fail("adopted another home's daemon"),
    )
    seen = _wire_spawn(monkeypatch)

    assert storage._ensure_runtime_daemon(object(), str(config), "error") == str(config)

    assert seen == {"spawned": [str(config)], "spawn_port": [chosen], "waited": [chosen]}
    assert storage._resolve_runtime_port(str(config)) == chosen
    assert storage._live_client_pids() == [os.getpid()]


def test_connect_or_spawn_retires_its_own_daemon_when_another_holds_the_port(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in_daemon: Any
) -> None:
    """This home's daemon runs but does not hold the port: it is killed by its pidfile
    and a fresh one spawns on a private block; the port's daemon is left alone."""
    cte_dir = tmp_path / "cte"
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: cte_dir)
    config = _generated_config(cte_dir, _FOREIGN_PORT)
    _write_pidfile(stand_in_daemon.pid)
    chosen = runtime_ownership.private_port_candidates(state)[0]
    monkeypatch.setattr(runtime_ownership, "_block_free", lambda base: base == chosen)
    monkeypatch.setattr(storage, "_runtime_alive", lambda port: port == _FOREIGN_PORT)
    seen = _wire_spawn(monkeypatch)

    assert storage._ensure_runtime_daemon(object(), str(config), "error") == str(config)

    assert stand_in_daemon.wait(timeout=10) is not None  # ours: killed
    assert seen["spawned"] == [str(config)]
    assert seen["waited"] == [chosen]


def test_connect_or_spawn_refuses_typed_for_a_hand_written_config(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    config = _generated_config(tmp_path / "hand", _FOREIGN_PORT)
    original = config.read_bytes()
    monkeypatch.setattr(storage, "_runtime_alive", lambda port: port == _FOREIGN_PORT)
    seen = _wire_spawn(monkeypatch)

    with pytest.raises(ClioCoreForeignDaemonError):
        storage._ensure_runtime_daemon(object(), str(config), "error")

    assert seen["spawned"] == []
    assert config.read_bytes() == original
    assert storage._client_registered is False
    assert os.getpid() not in storage._live_client_pids()


def test_connect_or_spawn_attaches_to_the_listener_its_own_daemon_holds(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: socket.socket
) -> None:
    """The real ownership check: the pidfile process holds the listener, so it is ours."""
    port = _port(listener)
    config = tmp_path / "cte.yaml"
    config.write_text(f"networking:\n  port: {port}\n", encoding="utf-8")
    _write_pidfile(os.getpid())  # this process holds the listener
    clio_core_daemon_version.record_daemon_version(
        state, daemon_binary="clio_run", config_path=str(config)
    )
    # The pidfile names this test process: a regression into the retire path must fail
    # the test, not terminate it.
    monkeypatch.setattr(
        runtime_stop, "kill_daemon_pidfile", lambda: pytest.fail("retired its own daemon")
    )
    seen = _wire_spawn(monkeypatch)

    assert storage._ensure_runtime_daemon(object(), str(config), "error") == str(config)

    assert seen["spawned"] == []
    assert storage._live_client_pids() == [os.getpid()]
