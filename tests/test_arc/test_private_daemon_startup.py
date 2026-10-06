"""The suite's private clio-core daemon: bounded shared memory, fast and loud start failures."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from clio_agent.arc.init_degradation import classify_init_failure
from clio_agent.arc.runtime_crash import (
    DaemonSpawnFailed,
    DaemonStillStarting,
    daemon_start_work,
    wait_for_spawned_daemon,
    watch_daemon_process,
)
from tests._cte_isolation import (
    PRIVATE_MAIN_SEGMENT,
    PRIVATE_METADATA_SEGMENT,
    PRIVATE_QUEUE_DEPTH,
    PRIVATE_RAM_BDEV,
    isolate_cte_env,
    private_daemon_failure_report,
)

_SHM_FAILURE = (
    "posix_shm_mmap.h:94 ERROR 25460 shm_init shm_open failed: No error\n"
    "clio_run_cmd_runtime_start.cc:190 ERROR 25460 RuntimeStart Failed to initialize "
    "Clio runtime\n"
)


def test_private_daemon_config_bounds_its_shared_memory(tmp_path: Path) -> None:
    root = tmp_path / "clio-agent-cte-1-unit"
    root.mkdir()

    config = isolate_cte_env(root, {}).config_path.read_text(encoding="utf-8")

    runtime = config.split("runtime:\n", 1)[1].split("compose:", 1)[0]
    assert f"queue_depth: {PRIVATE_QUEUE_DEPTH}" in runtime
    assert f'main_segment_size: "{PRIVATE_MAIN_SEGMENT}"' in runtime
    assert f'metadata_segment_size: "{PRIVATE_METADATA_SEGMENT}"' in runtime
    ram_bdev = config.split("bdev_type: ram\n", 1)[1].splitlines()[0]
    assert ram_bdev.strip() == f'capacity: "{PRIVATE_RAM_BDEV}"'


def test_a_daemon_that_dies_at_start_fails_at_once_with_its_own_reason(tmp_path: Path) -> None:
    log_path = tmp_path / "clio-runtime.log"
    log_path.write_text(_SHM_FAILURE, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"])
    watch_daemon_process(proc, log_path=log_path, state_dir=tmp_path)

    started = time.monotonic()
    with pytest.raises(DaemonSpawnFailed) as failed:
        wait_for_spawned_daemon(
            20001,
            alive=lambda _port: False,
            state_dir=tmp_path,
            work=daemon_start_work(proc.pid, log_path),
            no_progress_s=30.0,
            poll_s=0.05,
        )

    assert time.monotonic() - started < 10.0, "must not wait out the no-progress window"
    message = str(failed.value)
    assert "exited before binding port 20001" in message
    assert "Failed to initialize Clio runtime" in message
    assert classify_init_failure(failed.value) == "clio_core_daemon_spawn_failed"


def _stand_in(procs: list[subprocess.Popen[str]], source: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE, text=True)
    procs.append(proc)
    return proc


@pytest.fixture
def stand_ins():
    procs: list[subprocess.Popen[str]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


_STARTING = "import time\nend = time.monotonic() + 60\nwhile time.monotonic() < end:\n    pass\n"


def test_a_daemon_still_starting_is_waited_for_past_the_window(
    tmp_path: Path, stand_ins: list[subprocess.Popen[str]]
) -> None:
    """A slow machine: the daemon works (CPU) for several no-progress windows before it
    binds its port; the start succeeds instead of being failed and killed at 30 s.

    **Sabotage:** a fixed deadline of ``no_progress_s`` -> DaemonSpawnFailed at 0.3 s.
    """
    daemon = _stand_in(stand_ins, _STARTING)
    binds_at = time.monotonic() + 1.5
    wait_for_spawned_daemon(
        20004,
        alive=lambda _port: time.monotonic() >= binds_at,
        state_dir=tmp_path,
        work=daemon_start_work(daemon.pid, tmp_path / "clio-runtime.log"),
        no_progress_s=0.3,
        poll_s=0.05,
    )
    assert daemon.poll() is None


def test_a_daemon_that_never_binds_and_makes_no_progress_fails_typed(
    tmp_path: Path, stand_ins: list[subprocess.Popen[str]]
) -> None:
    idle = _stand_in(stand_ins, "import time; time.sleep(60)")
    time.sleep(0.5)  # past the interpreter start: from here the stand-in does nothing
    with pytest.raises(DaemonSpawnFailed, match="never bound port 20002 and made no progress"):
        wait_for_spawned_daemon(
            20002,
            alive=lambda _port: False,
            state_dir=tmp_path,
            work=daemon_start_work(idle.pid, tmp_path / "clio-runtime.log"),
            no_progress_s=0.3,
            poll_s=0.05,
        )


def test_a_daemon_still_working_at_the_ceiling_is_typed_still_starting(
    tmp_path: Path, stand_ins: list[subprocess.Popen[str]]
) -> None:
    daemon = _stand_in(stand_ins, _STARTING)
    with pytest.raises(DaemonStillStarting) as info:
        wait_for_spawned_daemon(
            20005,
            alive=lambda _port: False,
            state_dir=tmp_path,
            work=daemon_start_work(daemon.pid, tmp_path / "clio-runtime.log"),
            no_progress_s=0.2,
            ceiling_s=0.6,
            poll_s=0.05,
        )
    assert classify_init_failure(info.value) == "clio_core_daemon_start_ceiling"
    assert daemon.poll() is None  # the wait itself never kills


def test_a_daemon_that_binds_returns(tmp_path: Path) -> None:
    wait_for_spawned_daemon(
        20003,
        alive=lambda _port: True,
        state_dir=tmp_path,
        work=lambda: None,
        no_progress_s=1.0,
    )


def test_the_failure_report_names_the_daemon_reason_and_the_commit_charge(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clio-agent-cte-2-unit"
    root.mkdir()
    isolation = isolate_cte_env(root, {})
    (isolation.state_dir / "clio-runtime.log").write_text(_SHM_FAILURE, encoding="utf-8")

    report = private_daemon_failure_report(isolation)

    assert "this run fails instead" in report
    assert f"port {isolation.port}" in report
    assert "shm_open failed" in report
    if sys.platform.startswith("win"):
        assert "commit charge:" in report
