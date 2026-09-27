"""Daemon evidence for a crashed xdist worker's report (controller side).

When the hang guard (tests/_hang_guard.py) ends a worker, the usual suspect is that
worker's private clio-core daemon (tests/_cte_isolation.py): the binding blocks,
holding the GIL, on an RPC the daemon never answers. The stack says WHERE the test
waited; this says what the daemon was doing: whether it is still alive, its process
state, and the tail of its log. The dead worker's run root is still on disk when
xdist reports the crash (it is reaped at the next worker start or at session end).
"""

from __future__ import annotations

import os
from pathlib import Path

_LOG_TAIL_LINES = 40


def _daemon_state(pid: int) -> str:
    try:
        import psutil  # noqa: PLC0415

        proc = psutil.Process(pid)
        with proc.oneshot():
            return (
                f"alive, status={proc.status()}, cpu_times={tuple(proc.cpu_times())[:2]}, "
                f"rss={proc.memory_info().rss // (1 << 20)} MiB, threads={proc.num_threads()}"
            )
    except ImportError:
        return "unknown (psutil unavailable)"
    except Exception as exc:  # noqa: BLE001 - psutil raises several types for a gone/denied pid
        return f"not inspectable ({type(exc).__name__}: {exc})"


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(unreadable: {exc})"
    return "\n".join(text.splitlines()[-lines:])


def worker_daemon_report(worker_pid: int) -> str:
    """Describe the private daemon(s) of the run root owned by ``worker_pid``.

    Returns an empty string when no such run root exists (a worker that never booted a
    daemon, or a binding-free environment).
    """
    runtime_dir = os.environ.get("CLIO_TEST_RUNTIME_DIR")
    if not runtime_dir:
        return ""
    parent = Path(runtime_dir).parent
    sections: list[str] = []
    for state_dir in sorted(parent.glob(f"run-{worker_pid}-*/cte/*/clio-state")):
        pidfile = state_dir / "clio-runtime.pid"
        try:
            daemon_pid = int(pidfile.read_text(encoding="utf-8").split()[0])
            state = f"pid {daemon_pid}: {_daemon_state(daemon_pid)}"
        except (OSError, IndexError, ValueError) as exc:
            state = f"no readable pidfile ({exc})"
        log = state_dir / "clio-runtime.log"
        sections.append(
            f"private clio-core daemon of worker pid {worker_pid} ({state_dir}):\n"
            f"  {state}\n"
            f"  last {_LOG_TAIL_LINES} lines of {log.name}:\n{_tail(log, _LOG_TAIL_LINES)}"
        )
    return "\n".join(sections)
