"""Bounded remote commands through CLIO's shared, host-verified SSH connector."""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import paramiko

from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.storage.sftp import connect_route
from clio_agent.tools.servers.shell_output import (
    StreamCapture,
    compose_output_fields,
    shell_result_room,
)
from clio_agent.tools.servers.shell_spill_store import new_call_id, spill_directory


def _request_exec(
    channel: paramiko.Channel,
    command: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> str | None:
    """Bound the exec acknowledgement, which Paramiko's read timeout does not bound."""
    finished = threading.Event()
    aborted: list[str] = []

    def guard() -> None:
        try:
            while not finished.wait(0.02):
                reason = (
                    "cancelled"
                    if cancelled is not None and cancelled()
                    else "timeout"
                    if time.monotonic() >= deadline
                    else ""
                )
                if reason:
                    aborted.append(reason)
                    channel.close()
                    return
        finally:
            # A failed cancellation checker must unblock exec acknowledgement.
            # Future.result below propagates its original exception to the caller.
            if not finished.is_set():
                channel.close()

    # The cancellation checker is session-context dependent. Do not consult it
    # from an empty thread context or borrow another session's cancellation.
    copied = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="clio-ssh-deadline") as executor:
        watcher = executor.submit(copied.run, guard)
        try:
            channel.exec_command(command)
        except paramiko.SSHException:
            if not aborted:
                raise
        finally:
            finished.set()
            watcher.result()
    return aborted[0] if aborted else None


def execute_ssh_command(
    route: SshRoute,
    command: str,
    *,
    root: Path,
    session_id: str,
    timeout_s: float,
    max_output_bytes: int,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Drain both remote streams, preserving oversize output in session-owned spills.

    The caller owns approval before entering this function. Closing a timed-out
    or cancelled channel ends the local SSH request; it does not prove all remote
    descendants stopped. The remote account's OS permissions govern execution.
    """
    call_id = new_call_id()
    folder = spill_directory(root, session_id=session_id)
    captures = [
        StreamCapture(
            name, inline_limit=max_output_bytes, spill_path=folder / f"{call_id}.{name}.txt"
        )
        for name in ("stdout", "stderr")
    ]
    channel = None
    client = connect_route(route)
    stop_reason = "completed"
    exit_code: int | None = None
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionError("The verified SSH connection is no longer active")
        deadline = time.monotonic() + timeout_s
        channel = transport.open_session(timeout=timeout_s)
        channel.settimeout(timeout_s)
        abort = _request_exec(channel, command, deadline=deadline, cancelled=cancelled)
        if abort:
            stop_reason = abort
        else:
            channel.shutdown_write()
        while abort is None:
            if cancelled is not None and cancelled():
                stop_reason = "cancelled"
                break
            if time.monotonic() >= deadline:
                stop_reason = "timeout"
                break
            progressed = False
            # One bounded chunk per stream per iteration prevents a busy stdout
            # from starving stderr, cancellation or the command deadline.
            if channel.recv_ready():
                captures[0].feed(channel.recv(65536))
                progressed = True
            if channel.recv_stderr_ready():
                captures[1].feed(channel.recv_stderr(65536))
                progressed = True
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                exit_code = channel.recv_exit_status()
                if exit_code == -1:
                    stop_reason = "exit_status_unavailable"
                    exit_code = None
                break
            if not progressed:
                time.sleep(0.02)
    finally:
        if channel is not None:
            channel.close()
        client.close()
        for capture in captures:
            capture.finish()
    base = {
        "command": command,
        "ssh_route": route.model_dump(),
        "exit_code": exit_code,
        "status": "completed" if stop_reason == "completed" and exit_code == 0 else "failed",
        "stop_reason": stop_reason,
        "timeout_s": timeout_s,
    }
    if stop_reason != "completed":
        base["remote_process_state"] = "Unknown; closing SSH does not prove descendants stopped"
    return compose_output_fields(
        base,
        captures,
        root=root,
        budget=shell_result_room(),
    )
