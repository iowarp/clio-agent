"""A clio-core daemon whose every client crashed is replaced, never adopted.

clio-core 2.3.x can be left half-alive by a client killed between two chunks of one
shared-memory message: the worker draining that ring spins on the half message, the
port still accepts, and the next client's handshake is never answered. So
connect-or-spawn stops a daemon no live client uses once a client crashed on it, and
spawns a fresh one (``clio_core_daemon_replaced_after_client_crash``).

Binding-free: the registry and pidfile are real files in a private state dir, the
"daemon" is a real child process, and the stop/spawn are faked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from clio_agent.arc import crashed_clients, runtime_stop, storage
from clio_agent.arc.runtime_stop import StopOutcome
from clio_agent.runtime.clio_core_replacement_health import probe_clio_core_daemon_replacement


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private runtime state dir (lock, pidfile, client registry) for this test only."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(state_dir))
    monkeypatch.setattr(storage, "_client_registered", False)
    crashed_clients.reset_daemon_replacement()
    yield state_dir
    crashed_clients.reset_daemon_replacement()


@pytest.fixture()
def daemon(state: Path) -> Any:
    """A real child process standing in for ``clio_run``, named by the pidfile."""
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter, fixed argv
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ctime = psutil.Process(proc.pid).create_time()
    storage._daemon_pidfile().write_text(f"{proc.pid} {ctime!r}", encoding="utf-8")
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _register(pid: int, ctime: float | None) -> None:
    reg = storage._client_registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    (reg / str(pid)).write_text("" if ctime is None else repr(ctime), encoding="utf-8")


def _crashed_client() -> int:
    """A PID whose process is gone (a client that died without deregistering)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait(timeout=30)
    return proc.pid


def _stop_by_killing(proc: Any, calls: list[dict[str, Any]]) -> Any:
    def stop(config_path: str, log_level: str, *, ceiling_s: float | None = None) -> StopOutcome:
        calls.append({"config": config_path, "level": log_level, "ceiling_s": ceiling_s})
        proc.kill()
        return StopOutcome(stopped=True, path="clean_stop")

    return stop


def test_census_splits_live_from_crashed_and_prunes_the_crashed(state: Path) -> None:
    me = os.getpid()
    dead = _crashed_client()
    _register(me, storage._proc_create_time(me))
    _register(dead, 123.0)

    live, crashed = crashed_clients.client_census()

    assert live == [me]
    assert crashed == [dead]
    assert not (storage._client_registry_dir() / str(dead)).exists()
    assert storage._live_client_pids() == [me]  # the registry's one reader agrees


def test_a_daemon_every_client_crashed_on_is_stopped_with_a_bounded_stop(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage: return early when ``crashed`` is non-empty -> the daemon keeps running."""
    dead = _crashed_client()
    _register(dead, 123.0)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", _stop_by_killing(daemon, calls))
    monkeypatch.setenv("CLIO_ARC_LIVENESS_STOP_NO_PROGRESS_S", "7")

    assert crashed_clients.replace_daemon_left_by_crashed_clients("cfg.yaml", "error") is True

    assert calls == [{"config": "cfg.yaml", "level": "error", "ceiling_s": 7.0}]
    record = crashed_clients.daemon_replacement()
    assert record["reason"] == crashed_clients.DAEMON_REPLACED_AFTER_CLIENT_CRASH
    assert record["daemon_pid"] == daemon.pid
    assert record["crashed_clients"] == [dead]
    assert record["stop_path"] == "clean_stop"
    assert record["exited"] is True


def test_a_daemon_a_live_client_still_uses_is_left_alone(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(_crashed_client(), 123.0)
    parent = os.getppid()
    _register(parent, storage._proc_create_time(parent))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", _stop_by_killing(daemon, calls))

    assert crashed_clients.replace_daemon_left_by_crashed_clients("cfg.yaml", "error") is False

    assert calls == []
    assert daemon.poll() is None
    assert crashed_clients.daemon_replacement() == {}


def test_a_daemon_whose_clients_all_released_it_cleanly_is_adopted(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No registry entry left behind means no crash: the daemon is adopted as before."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", _stop_by_killing(daemon, calls))

    assert crashed_clients.replace_daemon_left_by_crashed_clients("cfg.yaml", "error") is False
    assert calls == []


def test_no_running_daemon_means_nothing_to_replace(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(_crashed_client(), 123.0)
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: False)
    monkeypatch.setattr(
        runtime_stop, "stop_runtime_daemon", lambda *a, **k: pytest.fail("nothing to stop")
    )

    assert crashed_clients.replace_daemon_left_by_crashed_clients("cfg.yaml", "error") is False


def test_connect_or_spawn_spawns_fresh_instead_of_adopting_after_a_client_crash(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restart path: the killed server's entry is left; the next start replaces.

    Sabotage: drop the ``replace_daemon_left_by_crashed_clients`` call from
    ``_ensure_runtime_daemon`` -> the listening daemon is adopted and nothing spawns.
    """
    config = tmp_path / "cte.yaml"
    config.write_text("networking:\n  port: 29997\n", encoding="utf-8")
    _register(_crashed_client(), 123.0)
    listening = {"now": True}
    spawned: list[str] = []
    calls: list[dict[str, Any]] = []
    kill = _stop_by_killing(daemon, calls)

    def stop(config_path: str, log_level: str, *, ceiling_s: float | None = None) -> StopOutcome:
        listening["now"] = False
        return kill(config_path, log_level, ceiling_s=ceiling_s)

    def spawn(_core: object, cfg: str, _level: str) -> None:
        spawned.append(cfg)
        listening["now"] = True

    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: listening["now"])
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", stop)
    monkeypatch.setattr(storage, "_spawn_runtime_daemon", spawn)
    monkeypatch.setattr(storage, "_wait_for_daemon_start", lambda _port, _state: None)

    effective = storage._ensure_runtime_daemon(object(), str(config), "error")

    assert effective == str(config)
    assert len(calls) == 1
    assert spawned == [str(config)]
    assert storage._live_client_pids() == [os.getpid()]
    storage._deregister_client()


def test_the_replacement_is_a_typed_health_row() -> None:
    assert probe_clio_core_daemon_replacement(record={}) == []
    record = {
        "reason": crashed_clients.DAEMON_REPLACED_AFTER_CLIENT_CRASH,
        "daemon_pid": 41,
        "crashed_clients": [40],
        "stop_path": "stall_kill",
        "exited": True,
        "seconds": 3.5,
    }

    [row] = probe_clio_core_daemon_replacement(record=record)

    assert row.name == "clio_core_daemon_replaced"
    assert row.details["reason"] == crashed_clients.DAEMON_REPLACED_AFTER_CLIENT_CRASH
    assert "stall_kill" in row.summary
    assert row.required is False
