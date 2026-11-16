"""Wait for a server ``clio_agent.serve`` spawned to stop on its own.

A server a script started stops itself when its last attached client leaves
(``gact/attached_clients.py``). The script that started it then waits here for the
process tree to exit, so nothing outlives the script, killing it only if it stops
making progress -- typed and logged, as :func:`clio_agent.serve.stop_server` does.
"""

from __future__ import annotations

import logging

from clio_agent.serve import (
    _pid_alive,
    _pidfile_path,
    _read_pidfile,
    _record_action,
    _remove_pidfile,
    _serve_reason,
    _terminate_tree,
    stop_server,
)
from clio_agent.server_port import configured_port

logger = logging.getLogger("clio_agent.serve")


def await_server_exit(port: int | None = None, *, no_progress_s: float | None = None) -> dict:
    """Wait for the server *we* spawned on ``port`` to exit on its own; kill it if it stalls.

    For a server that stops itself (its last attached client left): waits while its
    process tree makes progress, up to ``arc.liveness.max_wait_s``; a server that stops
    progressing is killed, typed ``killed`` with ``kill_reason``. Returns the structured
    note (``stopped``, ``killed``, or :func:`stop_server`'s no-op notes).
    """
    import psutil  # noqa: PLC0415

    from clio_agent.arc import daemon_progress, runtime_stop  # noqa: PLC0415
    from clio_agent.runtime.progress import ProcessTreeWork  # noqa: PLC0415

    port = configured_port() if port is None else port
    pidfile = _pidfile_path(port)
    record = _read_pidfile(pidfile) or {}
    pid, created = record.get("pid"), record.get("create_time")
    if not record.get("spawned_by_us") or not isinstance(pid, int) or created is None:
        return stop_server(port)  # nothing we can verify: the same typed no-op notes
    if not _pid_alive(pid, created):
        _remove_pidfile(pidfile)
        return _record_action(_serve_reason("stopped", pid=pid))

    def exited_within(timeout: float) -> bool:
        try:
            psutil.Process(pid).wait(timeout=timeout)
        except psutil.TimeoutExpired:
            return False
        except psutil.NoSuchProcess:
            return True
        return True

    window = no_progress_s if no_progress_s is not None else runtime_stop.stop_no_progress_s()
    exited = daemon_progress.wait_while_progressing(
        exited_within,
        slice_s=window,
        op_name=f"gact server exit after its last client (pid {pid})",
        work=ProcessTreeWork(pid).sample,
        ceiling_s=daemon_progress.max_wait_s(),
    )
    if not exited.done:
        logger.warning("gact server pid=%s did not exit (%s); killing it", pid, exited.reason)
        _terminate_tree(pid, record_create_time=created)
    _remove_pidfile(pidfile)
    if exited.done:
        return _record_action(_serve_reason("stopped", pid=pid))
    return _record_action(_serve_reason("killed", pid=pid, kill_reason=exited.reason))
