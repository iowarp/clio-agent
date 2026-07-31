"""Failing-first pins for #1148: hermetic CTE shm namespace + loud daemon-crash surfacing.

Root cause proven by A/B repro: clio-core keys its IPC shared-memory segments on
``${USER}`` only (unset on Windows), so every daemon on the box shared one namespace
and concurrent private daemons corrupted each other (user-visible clio_run.exe
access-violation dialogs; mid-suite ``ClioCoreRuntimeLostError``; the host daemon
dying). Two invariants land together:

* ``isolate_cte_env`` exports a UNIQUE per-run ``USER`` so each private daemon (and
  its in-process clients, which expand the same variable) lives in its own shm
  namespace — hermetic for real, and the host daemon never shares with tests.
* The daemon dies LOUDLY in our channels: a watcher on the spawned process writes a
  typed crash record on abnormal exit, and the liveness error names the crash
  (exit status + log tail) instead of a vague "not listening". No desktop dialog is
  ever suppressed; nothing gets quieter.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _cte_isolation import isolate_cte_env  # noqa: E402


def test_isolate_cte_env_sets_unique_shm_user(tmp_path: Path) -> None:
    """Each isolated run exports its own USER: private shm namespace per daemon."""

    env_a: dict[str, str] = {"USERNAME": "jaime"}
    env_b: dict[str, str] = {"USERNAME": "jaime"}
    isolate_cte_env(tmp_path / "run-a", env_a)
    isolate_cte_env(tmp_path / "run-b", env_b)

    assert env_a.get("USER"), "isolate_cte_env must export USER for shm namespacing"
    assert env_b.get("USER")
    assert env_a["USER"] != env_b["USER"], "two runs must not share an shm namespace"
    assert env_a["USER"] != "jaime", "the private namespace must differ from the host's"


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def test_watcher_writes_typed_crash_record_on_abnormal_exit(tmp_path: Path) -> None:
    """Abnormal daemon exit -> typed JSON crash record with exit status + log tail."""

    from clio_agent.arc.runtime_crash import crash_record_path, watch_daemon_process

    log_path = tmp_path / "clio-runtime.log"
    log_path.write_text("boot line 1\nboot line 2\nfatal: segment collision\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    watch_daemon_process(proc, log_path=log_path, state_dir=tmp_path)

    record_path = crash_record_path(tmp_path)
    _wait_for(record_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["exit_code"] == 3
    assert record["pid"] == proc.pid
    assert "fatal: segment collision" in record["log_tail"]
    assert record["crashed_at"]


def test_watcher_writes_no_record_on_clean_exit(tmp_path: Path) -> None:
    """A clean daemon exit (rc=0) is not a crash and writes no record."""

    from clio_agent.arc.runtime_crash import crash_record_path, watch_daemon_process

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    thread = watch_daemon_process(proc, log_path=tmp_path / "absent.log", state_dir=tmp_path)
    thread.join(timeout=10)
    assert not crash_record_path(tmp_path).exists()


def test_liveness_error_names_the_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'daemon not listening' error carries the crash facts when a record exists."""

    from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError, LivenessGate
    from clio_agent.arc.runtime_crash import crash_record_path

    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path))
    crash_record_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 4242,
                "exit_code": -1073741819,
                "exit_code_hex": "0xC0000005",
                "crashed_at": "2026-07-30T23:00:00Z",
                "log_tail": "fatal: segment collision",
                "log_path": str(tmp_path / "clio-runtime.log"),
            }
        ),
        encoding="utf-8",
    )

    gate = LivenessGate(probe=lambda _port: False)  # daemon definitively not listening
    with pytest.raises(ClioCoreRuntimeLostError) as exc_info:
        gate.ensure_live(reconnect=lambda: (_ for _ in ()).throw(RuntimeError("no")))

    message = str(exc_info.value)
    assert "0xC0000005" in message, "the error must NAME the crash, not just 'not listening'"
    assert "segment collision" in message
    details = exc_info.value.details
    assert details["daemon_crash"]["pid"] == 4242
