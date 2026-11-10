"""The clio-core daemon log never grows without bound, and a log flood is acted on.

On 2026-11-09 ``clio_run`` 2.4.0 looped forever on
``GlobalDeserialize::read_binary: Attempted to read beyond end of data``, logging one
line per iteration into an uncapped append-mode ``clio-runtime.log``: 128 GB in 53
minutes, disk full. The daemon now writes into a pipe read by a log pump that caps
and rotates the file, collapses repeats and records a flood; clio-agent surfaces the
flood and replaces the daemon when no live client uses it (or on an opt-in).

Binding-free: the "daemon" is a real child process (or a fake Popen), and the stop
is faked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from clio_agent.arc import runtime_log, runtime_log_flood, runtime_stop, storage
from clio_agent.arc import runtime_log_pump as pump_module
from clio_agent.arc.runtime_crash import _log_tail
from clio_agent.arc.runtime_log_pump import BoundedLog, FloodTracker, pump
from clio_agent.arc.runtime_stop import StopOutcome
from clio_agent.runtime.clio_core_log_health import probe_clio_core_log_flood
from clio_agent.runtime.status import IntegrationState

FLOOD_LINE = (
    b"/Users/runner/work/clio-core/clio-core/context-transport-primitives/data_structures/"
    b"include/clio_ctp/data_structures/serialization/global_serialize.h:259 ERROR 2705160 "
    b"read_binary GlobalDeserialize::read_binary: Attempted to read beyond end of data\n"
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TickingSource:
    """A readline source that advances a fake clock by ``step`` per line."""

    def __init__(self, lines: list[bytes], clock: _Clock, step: float) -> None:
        self._lines = iter(lines)
        self._clock = clock
        self._step = step

    def readline(self, _limit: int = -1) -> bytes:
        self._clock.now += self._step
        return next(self._lines, b"")


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(state_dir))
    monkeypatch.setattr(storage, "_client_registered", False)
    runtime_log_flood.reset_flood_state()
    yield state_dir
    runtime_log_flood.reset_flood_state()


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


def _write_flood_record(state: Path, daemon_pid: int, *, age_s: float = 0.0) -> None:
    record = {
        "reason": "clio_core_daemon_log_flood",
        "daemon_pid": daemon_pid,
        "line": FLOOD_LINE.decode().strip(),
        "lines_per_s": 200000.0,
        "first_seen": time.time() - 60,
        "last_seen": time.time() - age_s,
    }
    runtime_log.flood_record_path(state).write_text(json.dumps(record), encoding="utf-8")


def _register_live_client(pid: int) -> None:
    reg = storage._client_registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    (reg / str(pid)).write_text(repr(storage._proc_create_time(pid)), encoding="utf-8")


# ---- the pump ----------------------------------------------------------------------


def test_identical_lines_are_collapsed_into_a_repeat_summary(tmp_path: Path) -> None:
    log = BoundedLog(tmp_path / "clio-runtime.log", 1 << 20)
    clock = _Clock()
    lines = [b"starting\n"] + [FLOOD_LINE] * 50_000 + [b"stopped\n"]
    pump(_TickingSource(lines, clock, 0.0), log, FloodTracker(None, None, 1000), clock=clock)
    log.close()

    text = (tmp_path / "clio-runtime.log").read_bytes()
    assert text.count(FLOOD_LINE) == 1
    assert b"previous line repeated 49999 more times" in text
    assert text.endswith(b"stopped\n")


def test_a_long_run_is_summarised_periodically_not_only_at_its_end(tmp_path: Path) -> None:
    log = BoundedLog(tmp_path / "clio-runtime.log", 1 << 20)
    clock = _Clock()
    lines = [FLOOD_LINE] * 3001  # 30 s of fake time at 0.01 s/line
    pump(_TickingSource(lines, clock, 0.01), log, FloodTracker(None, None, 1000), clock=clock)
    log.close()

    notes = (tmp_path / "clio-runtime.log").read_bytes().count(b"previous line repeated")
    assert notes >= 3


def test_distinct_lines_are_capped_with_one_rotation(tmp_path: Path) -> None:
    """Sabotage: drop the size check in ``BoundedLog.write`` -> the file grows to ~2 MB."""
    path = tmp_path / "clio-runtime.log"
    log = BoundedLog(path, 64 * 1024)
    clock = _Clock()
    lines = [b"line %d of a flood of distinct lines\n" % i for i in range(60_000)]
    pump(_TickingSource(lines, clock, 0.0), log, FloodTracker(None, None, 1000), clock=clock)
    log.close()

    rotated = path.with_name("clio-runtime.log.1")
    assert path.stat().st_size <= 64 * 1024
    assert rotated.stat().st_size <= 64 * 1024
    assert path.read_bytes().endswith(b"line 59999 of a flood of distinct lines\n")
    assert sorted(p.name for p in tmp_path.glob("clio-runtime.log*")) == [
        "clio-runtime.log",
        "clio-runtime.log.1",
    ]


def test_write_errors_drop_output_but_keep_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk (ENOSPC) must never stop the pump: the daemon would block on the pipe."""
    log = BoundedLog(tmp_path / "clio-runtime.log", 1 << 20)
    failing = {"on": True}

    def flaky_open(*args: Any, **kwargs: Any) -> Any:
        if failing["on"]:
            raise OSError(28, "No space left on device")
        return open(*args, **kwargs)

    monkeypatch.setattr(pump_module, "open", flaky_open, raising=False)
    clock = _Clock()
    read = pump(
        _TickingSource([b"a\n", b"b\n", b"c\n"], clock, 0.0),
        log,
        FloodTracker(None, None, 1000),
        clock=clock,
    )
    assert read == 3
    assert log.dropped_bytes == 6

    failing["on"] = False
    log._failing_since = time.monotonic() - 2.0  # past the one-second retry back-off
    log.write(b"d\n")
    log.close()
    text = (tmp_path / "clio-runtime.log").read_bytes()
    assert b"dropped 6 bytes after write errors" in text
    assert text.endswith(b"d\n")


def test_a_sustained_flood_writes_a_record_and_its_end_removes_it(tmp_path: Path) -> None:
    record = tmp_path / "clio-runtime-flood.json"
    pidfile = tmp_path / "clio-runtime.pid"
    pidfile.write_text("4242 1.0", encoding="utf-8")
    tracker = FloodTracker(record, pidfile, lines_per_s=100)
    for i in range(2000):  # 2000 lines in 10 s = 200 lines/s > 100
        tracker.observe(FLOOD_LINE, i * 0.0051)
    tracker.observe(FLOOD_LINE, 10.3)  # closes the window and refreshes the record
    found = json.loads(record.read_text(encoding="utf-8"))
    assert found["daemon_pid"] == 4242
    assert found["reason"] == "clio_core_daemon_log_flood"
    assert "read_binary" in found["line"]
    assert found["lines_per_s"] >= 100

    tracker.observe(b"quiet\n", 25.0)  # a window with one line ends the flood
    assert not record.exists()


def test_a_slow_steady_log_is_not_a_flood(tmp_path: Path) -> None:
    record = tmp_path / "clio-runtime-flood.json"
    tracker = FloodTracker(record, None, lines_per_s=1000)
    for i in range(100):
        tracker.observe(FLOOD_LINE, float(i))
    assert not tracker.active
    assert not record.exists()


def test_the_pump_process_caps_a_real_flood_and_exits_at_eof(state: Path) -> None:
    """End to end: a writer floods the pipe; the file stays capped; the pump exits."""
    log_path = state / "clio-runtime.log"
    sink = runtime_log.start_log_pump(log_path, 64 * 1024, state, state / "clio-runtime.pid")
    assert sink is not None and isinstance(sink.target, int)
    line = FLOOD_LINE.decode()
    writer = subprocess.Popen(  # noqa: S603 - fixed interpreter, fixed argv
        [
            sys.executable,
            "-c",
            "import sys\n"
            f"line = {line!r}\n"
            "for i in range(200000):\n"
            "    sys.stdout.write(line if i % 1000 else 'distinct %d\\n' % i)\n",
        ],
        stdin=subprocess.DEVNULL,
        stdout=sink.target,
        stderr=sink.target,
    )
    sink.close()
    assert writer.wait(timeout=60) == 0
    pump_proc = psutil.Process(sink.pump_pid)
    pump_proc.wait(timeout=30)  # EOF -> exit

    assert log_path.stat().st_size <= 64 * 1024
    text = log_path.read_text(encoding="utf-8")
    assert "previous line repeated" in text
    assert "distinct 199000" in text
    assert not runtime_log.flood_record_path(state).exists()


# ---- spawn wiring ------------------------------------------------------------------


def test_an_oversized_log_is_rotated_at_spawn(tmp_path: Path) -> None:
    log_path = tmp_path / "clio-runtime.log"
    log_path.write_bytes(b"x" * 5000)
    assert runtime_log.rotate_if_oversized(log_path, 4096) is True
    assert not log_path.exists()
    assert log_path.with_name("clio-runtime.log.1").stat().st_size == 5000
    log_path.write_bytes(b"x" * 10)
    assert runtime_log.rotate_if_oversized(log_path, 4096) is False


def test_a_bad_cap_never_lifts_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES", "0")
    assert runtime_log.resolve_log_max_bytes() == runtime_log.DEFAULT_LOG_MAX_BYTES
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES", "lots")
    assert runtime_log.resolve_log_max_bytes() == runtime_log.DEFAULT_LOG_MAX_BYTES
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES", "1048576")
    assert runtime_log.resolve_log_max_bytes() == 1048576


def _spawn_capturing(monkeypatch: pytest.MonkeyPatch, state: Path) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Proc:
        pid = 4321

    def popen(args: Any, **kwargs: Any) -> _Proc:
        captured["args"] = args
        captured["stdout"] = kwargs["stdout"]
        return _Proc()

    monkeypatch.setattr(
        "clio_agent.arc.storage.watch_daemon_process",
        lambda _proc, log_path, state_dir, *, log_offset: None,
    )
    monkeypatch.setattr("clio_agent.arc.storage._proc_create_time", lambda _pid: 1.0)
    monkeypatch.setattr(
        "clio_agent.arc.storage._runtime_launcher_path", lambda _core: str(state / "clio_run")
    )
    monkeypatch.setattr(
        "clio_agent.arc.storage.daemon_version.record_daemon_version", lambda *_a, **_k: None
    )
    monkeypatch.setattr("clio_agent.arc.storage.subprocess.Popen", popen)

    class _Core:
        @staticmethod
        def get_lib_dir() -> str:
            return str(state)

        @staticmethod
        def get_bin_dir() -> str:
            return str(state)

    storage._spawn_runtime_daemon(_Core(), "", "error")
    return captured


def test_the_daemon_writes_into_the_pump_pipe(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sabotage: hand the daemon ``open(log, 'ab')`` again -> stdout is a file, not the pipe."""
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        "clio_agent.arc.runtime_log.start_log_pump",
        lambda *_a: runtime_log.DaemonLogSink(target=write_fd, pump_pid=1),
    )
    _write_flood_record(state, 999)  # left by an earlier daemon
    captured = _spawn_capturing(monkeypatch, state)
    os.close(read_fd)

    assert captured["stdout"] == write_fd
    assert captured["args"][1:] == ["start"]
    with pytest.raises(OSError):
        os.fstat(write_fd)  # the parent's copy is closed once the daemon holds its own
    assert not runtime_log.flood_record_path(state).exists()


def test_without_a_pump_the_daemon_writes_a_spawn_capped_file(
    state: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log_path = state / "clio-runtime.log"
    log_path.write_bytes(b"y" * 9000)
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_LOG_MAX_BYTES", "4096")
    monkeypatch.setattr(runtime_log, "_PUMP_SCRIPT", state / "missing_pump.py")

    with caplog.at_level("WARNING", logger="clio_agent.arc.runtime_log"):
        captured = _spawn_capturing(monkeypatch, state)

    assert not isinstance(captured["stdout"], int)
    assert captured["stdout"].name == str(log_path)
    assert log_path.with_name("clio-runtime.log.1").stat().st_size == 9000
    assert "rotated oversized clio-core daemon log" in caplog.text


def test_a_pump_that_cannot_spawn_falls_back_loudly(
    state: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError(12, "Cannot allocate memory")

    monkeypatch.setattr("clio_agent.arc.runtime_log.subprocess.Popen", refuse)
    with caplog.at_level("WARNING", logger="clio_agent.arc.runtime_log"):
        sink = runtime_log.start_log_pump(state / "l.log", 4096, state, state / "p.pid")
    assert sink is None
    assert runtime_log.LOG_PUMP_UNAVAILABLE in caplog.text


def test_the_crash_tail_survives_a_rotation_and_stays_bounded(tmp_path: Path) -> None:
    log_path = tmp_path / "clio-runtime.log"
    log_path.write_bytes(b"last words\n")
    assert _log_tail(log_path, offset=10_000_000) == "last words"  # offset from before rotation
    log_path.write_bytes(b"z" * (1 << 20) + b"\nfinal line\n")
    tail = _log_tail(log_path, offset=0)
    assert tail.endswith("final line")
    assert len(tail) <= 64 * 1024


# ---- acting on a flood -------------------------------------------------------------


def test_only_a_fresh_record_for_the_pidfile_daemon_counts(state: Path, daemon: Any) -> None:
    _write_flood_record(state, daemon.pid)
    assert runtime_log_flood.active_flood()["daemon_pid"] == daemon.pid
    _write_flood_record(state, daemon.pid, age_s=runtime_log_flood.RECORD_FRESH_S + 5)
    assert runtime_log_flood.active_flood() == {}
    _write_flood_record(state, daemon.pid + 100_000)  # an earlier daemon's record
    assert runtime_log_flood.active_flood() == {}


def _stop_by_killing(proc: Any, calls: list[str]) -> Any:
    def stop(config_path: str, log_level: str, *, ceiling_s: float | None = None) -> StopOutcome:
        calls.append(config_path)
        proc.kill()
        return StopOutcome(stopped=True, path="clean_stop")

    return stop


def test_a_flooding_daemon_nobody_uses_is_replaced_at_attach(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage: return False when the record is active -> the spinner is adopted."""
    _write_flood_record(state, daemon.pid)
    calls: list[str] = []
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", _stop_by_killing(daemon, calls))

    assert runtime_log_flood.replace_flooding_daemon("cfg.yaml", "error") is True

    assert calls == ["cfg.yaml"]
    found = runtime_log_flood.flood_replacement()
    assert found["reason"] == runtime_log_flood.DAEMON_REPLACED_AFTER_LOG_FLOOD
    assert found["daemon_pid"] == daemon.pid
    assert found["exited"] is True
    rows = probe_clio_core_log_flood(active={})
    assert rows[0].state is IntegrationState.READY
    assert rows[0].details["reason"] == runtime_log_flood.DAEMON_REPLACED_AFTER_LOG_FLOOD


def test_a_flooding_daemon_with_live_clients_is_surfaced_not_stopped(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_flood_record(state, daemon.pid)
    _register_live_client(os.getpid())
    calls: list[str] = []
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", _stop_by_killing(daemon, calls))

    with caplog.at_level("ERROR", logger="clio_agent.arc.runtime_log_flood"):
        assert runtime_log_flood.replace_flooding_daemon("cfg.yaml", "error") is False
        assert runtime_log_flood.replace_flooding_daemon("cfg.yaml", "error") is False

    assert calls == []
    assert daemon.poll() is None
    assert caplog.text.count(runtime_log_flood.DAEMON_LOG_FLOOD) == 1  # once per flood
    rows = probe_clio_core_log_flood()
    assert rows[0].state is IntegrationState.DEGRADED
    assert rows[0].details["daemon_pid"] == daemon.pid


def test_the_opt_in_replaces_a_flooding_daemon_even_with_live_clients(
    state: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_flood_record(state, daemon.pid)
    _register_live_client(os.getpid())
    calls: list[str] = []
    monkeypatch.setattr(runtime_stop, "stop_runtime_daemon", _stop_by_killing(daemon, calls))
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_LOG_FLOOD_RESTART_LIVE", "1")

    runtime_log_flood._poll_once()  # the monitor path takes the spawn lock itself

    assert len(calls) == 1
    assert daemon.wait(timeout=10) is not None
    assert runtime_log_flood.flood_replacement()["live_clients"] == [os.getpid()]


def test_no_flood_means_no_row_and_no_stop(state: Path, daemon: Any) -> None:
    assert runtime_log_flood.replace_flooding_daemon("cfg.yaml", "error") is False
    assert probe_clio_core_log_flood() == []


def test_the_pump_imports_only_the_stdlib() -> None:
    """The pump runs as a script under ``python -I``: a clio_agent import would fail there."""
    source = Path(pump_module.__file__ or "").read_text(encoding="utf-8")
    imports = [ln for ln in source.splitlines() if ln.startswith(("import ", "from "))]
    assert imports and not [ln for ln in imports if "clio_agent" in ln]
