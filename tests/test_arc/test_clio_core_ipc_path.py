"""iowarp-core 2.5 refuses to start when its IPC socket path overflows ``sun_path``.

The daemon puts its socket at ``<memfd dir>/clio_<port>.ipc``; a path that does not
fit exits the daemon with status 1 and one ERROR line ("refusing to start
half-bound"). CLIO computes that path from the child's environment before the spawn
and fails with a typed reason instead (``clio_core_ipc_path_too_long``). The log pump
race that hid the daemon's ERROR line ("no daemon log output") is pinned here too.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from clio_agent.arc.clio_core_ipc_path import (
    CLIO_CORE_IPC_PATH_TOO_LONG,
    CLIO_CORE_MEMFD_DIR_UNSAFE,
    ClioCoreIpcPathTooLong,
    ClioCoreMemfdDirUnsafe,
    ipc_socket_path,
    ipc_socket_paths,
    require_ipc_path_fits,
    require_private_memfd_dir,
    sun_path_capacity,
)
from clio_agent.arc.init_degradation import ArcStoreUnavailableError
from clio_agent.arc.runtime_crash import DaemonSpawnFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _cte_isolation import isolate_cte_env  # noqa: E402


def test_socket_path_follows_user_then_memfd_override() -> None:
    assert ipc_socket_path({"USER": "ana"}, 9413, "linux") == "/tmp/clio_ana/clio_9413.ipc"
    env = {"USER": "ana", "CLIO_MEMFD_DIR": "/run/clio"}
    assert ipc_socket_path(env, 9413, "linux") == "/run/clio/clio_9413.ipc"


def test_macos_also_binds_the_local_zmq_socket() -> None:
    env = {"USER": "ana"}
    assert ipc_socket_paths(env, 9413, "darwin") == [
        "/tmp/clio_ana/clio_9413.ipc",
        "/tmp/clio_ana/clio_zmq_9416.ipc",
    ]
    assert ipc_socket_paths({**env, "CLIO_ZMQ_LOCAL_IPC": "0"}, 9413, "darwin") == [
        "/tmp/clio_ana/clio_9413.ipc"
    ]
    assert len(ipc_socket_paths({**env, "CLIO_ZMQ_LOCAL_IPC": "1"}, 9413, "linux")) == 2


def test_windows_verbatim_memfd_dir_is_normalized() -> None:
    env = {"CLIO_MEMFD_DIR": "\\\\?\\C:\\clio"}
    assert ipc_socket_path(env, 9413, "win32") == "C:\\clio/clio_9413.ipc"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX ownership check")
@pytest.mark.parametrize("kind", ["group_writable", "symlink", "file"])
def test_an_unsafe_memfd_dir_is_refused_typed(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "memfd"
    if kind == "group_writable":
        target.mkdir()
        target.chmod(0o770)
    elif kind == "symlink":
        (tmp_path / "real").mkdir(mode=0o700)
        target.symlink_to(tmp_path / "real")
    else:
        target.write_text("", encoding="utf-8")
    with pytest.raises(ClioCoreMemfdDirUnsafe) as caught:
        require_private_memfd_dir({"CLIO_MEMFD_DIR": str(target)})
    assert caught.value.degradation_reason == CLIO_CORE_MEMFD_DIR_UNSAFE
    unavailable = ArcStoreUnavailableError(error=caught.value, config_path="cte.yaml")
    assert unavailable.reason == CLIO_CORE_MEMFD_DIR_UNSAFE


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX ownership check")
def test_a_private_or_missing_memfd_dir_passes(tmp_path: Path) -> None:
    require_private_memfd_dir({"CLIO_MEMFD_DIR": str(tmp_path / "absent")})
    (tmp_path / "private").mkdir(mode=0o700)
    require_private_memfd_dir({"CLIO_MEMFD_DIR": str(tmp_path / "private")})


def test_capacity_is_platform_specific() -> None:
    assert sun_path_capacity("linux") == 108
    assert sun_path_capacity("win32") == 108
    assert sun_path_capacity("darwin") == 104


@pytest.mark.parametrize(
    ("user_len", "platform", "fits"),
    [
        # /tmp/clio_ (10) + USER + /clio_22483.ipc (15) must stay under sun_path;
        # 82 starts and 83 exits 1 on Linux against the real 2.5.0 daemon. macOS also
        # binds /clio_zmq_22486.ipc (19) and has a 104-byte sun_path.
        (82, "linux", True),
        (83, "linux", False),
        (74, "darwin", True),
        (75, "darwin", False),
    ],
)
def test_boundary_matches_the_daemon(user_len: int, platform: str, fits: bool) -> None:
    env = {"USER": "u" * user_len}
    if fits:
        require_ipc_path_fits(env, 22483, platform)
        return
    with pytest.raises(ClioCoreIpcPathTooLong) as caught:
        require_ipc_path_fits(env, 22483, platform)
    assert caught.value.degradation_reason == CLIO_CORE_IPC_PATH_TOO_LONG
    assert "CLIO_MEMFD_DIR" in str(caught.value)


def test_too_long_is_a_typed_spawn_failure_for_the_arc_store() -> None:
    error = ClioCoreIpcPathTooLong("/tmp/clio_x/clio_1.ipc", 108)
    assert isinstance(error, DaemonSpawnFailed)
    unavailable = ArcStoreUnavailableError(error=error, config_path="cte.yaml")
    assert unavailable.reason == CLIO_CORE_IPC_PATH_TOO_LONG


def test_spawn_refuses_before_launching(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard runs on the child env (USER pinned) before any process starts."""
    from clio_agent.arc import storage

    launched: list[object] = []
    monkeypatch.setenv("USER", "u" * 120)
    monkeypatch.delenv("CLIO_MEMFD_DIR", raising=False)
    monkeypatch.delenv("CLIO_CORE_USER", raising=False)
    monkeypatch.setattr(storage, "_runtime_launcher_path", lambda _core: "/bin/false")
    monkeypatch.setattr(storage, "_resolve_runtime_port", lambda _cfg: 22483)
    monkeypatch.setattr(storage, "pin_core_child_env", lambda _env: None)
    monkeypatch.setattr(storage.subprocess, "Popen", lambda *a, **k: launched.append(a))

    class _Core:
        @staticmethod
        def get_lib_dir() -> str:
            return "/nonexistent"

    with pytest.raises(ClioCoreIpcPathTooLong):
        storage._spawn_runtime_daemon(_Core(), "cte.yaml", "warning")
    assert launched == []


def test_nested_isolation_keeps_user_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe child isolating again must not stack a second suffix onto USER."""
    monkeypatch.setattr("_cte_isolation.reserve_port_block", lambda: 22475)
    env: dict[str, str] = {"USER": "root"}
    isolate_cte_env(tmp_path / "outer", env)
    first = env["USER"]
    isolate_cte_env(tmp_path / "inner", env)
    assert env["USER"] != first
    assert len(env["USER"]) == len(first)
    require_ipc_path_fits(env, 22475, "darwin")


def test_crash_tail_waits_for_the_log_pump(tmp_path: Path) -> None:
    """A daemon that dies at once still has its last line in the pump when it exits."""
    from clio_agent.arc.runtime_crash import crash_record_path, watch_daemon_process

    log_path = tmp_path / "clio-runtime.log"
    log_path.write_text("", encoding="utf-8")
    drained = threading.Event()
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"])
    watch_daemon_process(proc, log_path=log_path, state_dir=tmp_path, log_drained=drained)
    proc.wait()
    time.sleep(0.2)
    assert not crash_record_path(tmp_path).exists(), "tail read before the pump drained"
    log_path.write_text("ERROR refusing to start half-bound.\n", encoding="utf-8")
    drained.set()
    deadline = time.monotonic() + 10
    while not crash_record_path(tmp_path).exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    record = json.loads(crash_record_path(tmp_path).read_text(encoding="utf-8"))
    assert "refusing to start half-bound" in record["log_tail"]
    assert record["log_tail_settled"] is True
