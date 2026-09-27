"""A failed clio-core startup must not leave a ``clio_run`` behind (#1401).

Two ways a start could leak the daemon:

* the spawned daemon never binds its port: the spawner raised, but the daemon
  process kept running, and the port-based clean stop could not see it because
  its port was already free;
* the daemon came up but the native attach, the CTE init, or the post-attach
  probe failed: the process only deregistered, so a daemon it had just spawned
  ran on with no client at all.

Binding-free: the "daemon" is a real child process (so the kill is proven on a
real PID), and the native modules are faked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from clio_agent.arc import clio_core_attach, runtime_stop, storage
from clio_agent.arc.runtime_crash import DaemonSpawnFailed


@pytest.fixture()
def private_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the lock / pidfile / client registry at a scratch dir for this test only."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(state))
    monkeypatch.setattr(storage, "_client_registered", False)
    return state


@pytest.fixture()
def stand_in_daemon() -> Any:
    """A real, harmless child process standing in for a ``clio_run`` that never binds."""
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


def _registered_pids(state: Path) -> list[str]:
    registry = state / "clio-runtime.clients"
    return sorted(entry.name for entry in registry.iterdir()) if registry.is_dir() else []


def test_daemon_that_never_binds_is_killed_and_unregistered(
    private_state_dir: Path, stand_in_daemon: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The spawner's failure kills the daemon it spawned; the process holds no vote."""
    config = tmp_path / "cte.yaml"
    config.write_text("networking:\n  port: 29999\n", encoding="utf-8")

    def fake_spawn(_core: object, _cfg: str, _level: str) -> None:
        ctime = psutil.Process(stand_in_daemon.pid).create_time()
        storage._daemon_pidfile().write_text(f"{stand_in_daemon.pid} {ctime!r}", encoding="utf-8")

    monkeypatch.setattr(storage, "_spawn_runtime_daemon", fake_spawn)
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: False)
    monkeypatch.setattr(storage, "_RUNTIME_START_TIMEOUT_S", 0.3)

    with pytest.raises(DaemonSpawnFailed, match="never bound port 29999"):
        storage._ensure_runtime_daemon(object(), str(config), "error")

    stand_in_daemon.wait(timeout=10)  # raises TimeoutExpired if the daemon was left running
    assert stand_in_daemon.returncode is not None
    assert str(os.getpid()) not in _registered_pids(private_state_dir)
    assert not storage._daemon_pidfile().exists()


def test_spawn_that_raises_before_a_daemon_exists_still_unregisters(
    private_state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launcher that cannot even start leaves no registration behind."""

    def failing_spawn(_core: object, _cfg: str, _level: str) -> None:
        raise RuntimeError("clio-core runtime launcher (clio_run) not found")

    monkeypatch.setattr(storage, "_spawn_runtime_daemon", failing_spawn)
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: False)

    with pytest.raises(RuntimeError, match="not found"):
        storage._ensure_runtime_daemon(object(), str(tmp_path / "cte.yaml"), "error")

    assert str(os.getpid()) not in _registered_pids(private_state_dir)


def test_failed_attach_as_last_client_stops_the_daemon(
    private_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no other live client, a failed attach stops the daemon it would have used."""
    storage._register_client()
    stopped: list[tuple[str, str]] = []
    monkeypatch.setattr(storage, "_stop_runtime_daemon", lambda c, lvl: stopped.append((c, lvl)))

    runtime_stop.release_failed_attach("cte.yaml", "error")

    assert stopped == [("cte.yaml", "error")]
    assert str(os.getpid()) not in _registered_pids(private_state_dir)


def test_failed_attach_leaves_a_shared_daemon_to_its_live_clients(
    private_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another live client keeps the daemon: only this process's vote is dropped."""
    storage._register_client()
    stopped: list[str] = []
    monkeypatch.setattr(storage, "_stop_runtime_daemon", lambda c, _lvl: stopped.append(c))
    monkeypatch.setattr(storage, "_live_client_pids", lambda: [os.getpid() + 1])

    runtime_stop.release_failed_attach("cte.yaml", "error")

    assert stopped == []
    assert str(os.getpid()) not in _registered_pids(private_state_dir)


def _cte(*, client_ok: bool, init_error: Exception | None, calls: list[str]) -> SimpleNamespace:
    def clio_init(_mode: object, _with_runtime: bool) -> bool:
        calls.append("clio_init")
        return client_ok

    def initialize_cte(_cfg: str, _query: object) -> None:
        calls.append("initialize_cte")
        if init_error is not None:
            raise init_error

    return SimpleNamespace(
        clio_init=clio_init,
        RuntimeMode=SimpleNamespace(kClient="kClient"),
        initialize_cte=initialize_cte,
        PoolQuery=SimpleNamespace(Dynamic=lambda: "dynamic"),
    )


def test_failed_cte_init_releases_exactly_once() -> None:
    """A CTE init failure after a good attach releases the client once and re-raises."""
    calls: list[str] = []
    released: list[bool] = []
    cte = _cte(client_ok=True, init_error=RuntimeError("pool create failed"), calls=calls)

    with pytest.raises(RuntimeError, match="pool create failed"):
        clio_core_attach.attach_and_initialize(
            cte,
            config_path="c.yaml",
            port=1,
            settle_s=0.0,
            on_failure=lambda: released.append(True),
        )

    assert calls == ["clio_init", "initialize_cte"]
    assert released == [True]


def test_failed_native_attach_releases_once_and_skips_cte_init() -> None:
    calls: list[str] = []
    released: list[bool] = []
    cte = _cte(client_ok=False, init_error=None, calls=calls)

    with pytest.raises(clio_core_attach.ClioCoreAttachError):
        clio_core_attach.attach_and_initialize(
            cte,
            config_path="c.yaml",
            port=1,
            settle_s=0.0,
            on_failure=lambda: released.append(True),
        )

    assert calls == ["clio_init"]
    assert released == [True]


def test_successful_attach_and_init_never_release() -> None:
    calls: list[str] = []
    released: list[bool] = []
    cte = _cte(client_ok=True, init_error=None, calls=calls)

    clio_core_attach.attach_and_initialize(
        cte, config_path="c.yaml", port=1, settle_s=0.0, on_failure=lambda: released.append(True)
    )

    assert calls == ["clio_init", "initialize_cte"]
    assert released == []
