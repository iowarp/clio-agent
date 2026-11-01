"""The ``run_code`` kernels: one persistent Python process per ``(session, react scope)``.

A kernel is started on a script's first call in its scope and kept for the next, so
variables and imports persist. It runs the execution runtime's Python
(``CLIO_EXECUTION_PYTHON``, published when the workspace's execution runtime is
prepared) as ``python -I kernel_main.py``, composed by :func:`sandbox.wrap_confined`
with the shell's profile and write roots and the shell's environment scrub (no CLIO
tokens or plugin secrets). Without a prepared runtime it runs the server's own Python
and the trace says so (``code_kernel_server_python``).

Each kernel owns a fresh process group (Job on Windows), so stopping it stops what the
script started. Kernels end when their scope's session closes
(:func:`close_session`), after ``code.idle_minutes`` without a script (the reaper),
when a script is cancelled or times out (the state is gone; the next script starts a
new kernel), and at process exit.
"""

from __future__ import annotations

import atexit
import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace

__all__ = [
    "KERNEL_SERVER_PYTHON",
    "Kernel",
    "KernelError",
    "close_all",
    "close_session",
    "kernel_for",
]

#: Typed trace reason: no prepared execution runtime; the kernel runs the server's Python.
KERNEL_SERVER_PYTHON = "code_kernel_server_python"
_MAIN = Path(__file__).with_name("kernel_main.py")
_START_TIMEOUT_S = 60.0


class KernelError(RuntimeError):
    """The kernel could not start, or stopped answering."""


def idle_minutes() -> float:
    """Minutes a kernel waits for its next script before it is stopped."""
    return conf.resolve("code.idle_minutes", env="CLIO_CODE_IDLE_MINUTES", default=30.0, cast=float)


def _environment(workspace: Path) -> tuple[str, dict[str, str]]:
    from clio_agent.paths import workspace_state_dir  # noqa: PLC0415
    from clio_agent.runtime.execution_environment import shell_environment  # noqa: PLC0415

    prepared = shell_environment(workspace)
    python = prepared.get("CLIO_EXECUTION_PYTHON") or ""
    if not python:
        python = sys.executable
        trace.event("CODE", "%s python=%s", KERNEL_SERVER_PYTHON, python)
    env = {**os.environ, **prepared, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    for internal in ("CLIO_AUTH_TOKEN", "CLIO_GACT_BEARER_TOKEN"):
        env.pop(internal, None)
    env = {k: v for k, v in env.items() if not k.startswith("CLIO_SECRET_")}
    env["CLIO_AGENT_WORKSPACE_STATE_DIR"] = str(workspace_state_dir(workspace))
    return python, env


class Kernel:
    """One kernel process and its protocol channel."""

    def __init__(self, key: tuple[str, str], workspace: Path) -> None:
        self.key = key
        self.workspace = workspace
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr: collections.deque[str] = collections.deque(maxlen=200)
        self.bound_digest = ""
        self.last_used = time.monotonic()
        self.lock = threading.Lock()  # one script at a time per kernel
        self.send_lock = threading.Lock()  # results come back from several call threads
        self.process: subprocess.Popen[bytes] | None = None
        self.tree: Any = None
        self.python = ""

    def start(self) -> None:
        from clio_agent.runtime import sandbox  # noqa: PLC0415
        from clio_agent.runtime.task_process_tree import (  # noqa: PLC0415
            TaskProcessTree,
            task_spawn_options,
        )

        python, env = _environment(self.workspace)
        self.python = python
        confined = sandbox.wrap_confined(
            python,
            ["-I", str(_MAIN)],
            write_roots=sandbox.effective_write_roots(
                sandbox.PROFILE_SHELL, workspace_root=str(self.workspace)
            ),
            net_policy=sandbox.NET_ALLOW_RECORD,
            profile=sandbox.PROFILE_SHELL,
            pdeathsig=True,
            cwd=self.workspace,
        )
        self.process = subprocess.Popen(  # noqa: S603 - the confined, resolved kernel argv
            [confined.command, *confined.args],
            cwd=str(self.workspace),
            env={**env, **confined.env_overlay},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **task_spawn_options(confined.popen_kwargs),
        )
        self.tree = TaskProcessTree(self.process.pid)
        threading.Thread(target=self._read, name="code-kernel-out", daemon=True).start()
        threading.Thread(target=self._drain, name="code-kernel-err", daemon=True).start()
        ready = self.receive(_START_TIMEOUT_S)
        if not ready or ready.get("type") != "ready":
            self.stop("start_failed")
            raise KernelError(f"the Python kernel did not start: {self.diagnostics()}")
        trace.event("CODE", "kernel started key=%s pid=%s", self.key, ready.get("pid"))

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                self.stderr.append(line.decode("utf-8", "replace").rstrip())
        self.messages.put(None)

    def _drain(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(line.decode("utf-8", "replace").rstrip())

    def diagnostics(self) -> str:
        code = self.process.poll() if self.process is not None else None
        tail = "\n".join(list(self.stderr)[-20:])
        return f"exit code {code}; {tail}" if tail else f"exit code {code}"

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def send(self, message: dict[str, Any]) -> None:
        if not self.alive() or self.process is None or self.process.stdin is None:
            raise KernelError(f"the Python kernel stopped: {self.diagnostics()}")
        line = (json.dumps(message, default=repr) + "\n").encode("utf-8")
        try:
            with self.send_lock:
                self.process.stdin.write(line)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise KernelError(f"the Python kernel stopped: {self.diagnostics()}") from exc

    def receive(self, timeout: float) -> dict[str, Any] | None:
        """The kernel's next message, ``None`` at end of stream; raises ``queue.Empty``."""
        message = self.messages.get(timeout=timeout)
        if message is None:
            self.messages.put(None)  # stay at end of stream for later readers
        return message

    def bind(self, table: dict[str, Any]) -> None:
        """Send the stub table when it changed since the last script."""
        digest = json.dumps(table, sort_keys=True, default=str)
        if digest == self.bound_digest:
            return
        self.send({"type": "bind", "stubs": table})
        answer = self.receive(_START_TIMEOUT_S)
        if not answer or answer.get("type") != "bound":
            raise KernelError(f"the Python kernel did not take its tools: {self.diagnostics()}")
        self.bound_digest = digest

    def stop(self, reason: str) -> None:
        """Stop the kernel and everything it started."""
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if self.tree is not None:
                self.tree.terminate()
            process.kill()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            trace.event("CODE", "kernel stop incomplete key=%s pid=%s", self.key, process.pid)
        finally:
            if self.tree is not None:
                self.tree.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        trace.event("CODE", "kernel stopped key=%s reason=%s", self.key, reason)


_KERNELS: dict[tuple[str, str], Kernel] = {}
_LOCK = threading.Lock()
_REAPER: threading.Thread | None = None


def kernel_for(session_id: str, scope: str, workspace: Path) -> Kernel:
    """The scope's running kernel, started when there is none (or it died)."""
    key = (session_id, scope)
    with _LOCK:
        kernel = _KERNELS.get(key)
        if kernel is not None and kernel.alive() and kernel.workspace == workspace:
            kernel.last_used = time.monotonic()
            return kernel
        if kernel is not None:
            kernel.stop("replaced")
        kernel = Kernel(key, workspace)
        _KERNELS[key] = kernel
        _start_reaper()
    kernel.start()
    return kernel


def discard(kernel: Kernel, reason: str) -> None:
    """Stop a kernel whose state can no longer be trusted (cancelled, timed out)."""
    with _LOCK:
        if _KERNELS.get(kernel.key) is kernel:
            del _KERNELS[kernel.key]
    kernel.stop(reason)


def close_session(session_id: str) -> None:
    """Stop every kernel of a session (it closed or was deleted)."""
    with _LOCK:
        doomed = [k for key, k in _KERNELS.items() if key[0] == session_id]
        for kernel in doomed:
            del _KERNELS[kernel.key]
    for kernel in doomed:
        kernel.stop("session_closed")


def close_all() -> None:
    """Stop every kernel (process exit)."""
    with _LOCK:
        doomed = list(_KERNELS.values())
        _KERNELS.clear()
    for kernel in doomed:
        kernel.stop("shutdown")


def _reap() -> None:
    while True:
        time.sleep(60)
        limit = idle_minutes() * 60
        now = time.monotonic()
        with _LOCK:
            idle = [
                k for k in _KERNELS.values() if now - k.last_used > limit and not k.lock.locked()
            ]
            for kernel in idle:
                del _KERNELS[kernel.key]
        for kernel in idle:
            kernel.stop("idle")


def _start_reaper() -> None:
    global _REAPER
    if _REAPER is None or not _REAPER.is_alive():
        _REAPER = threading.Thread(target=_reap, name="code-kernel-reaper", daemon=True)
        _REAPER.start()


atexit.register(close_all)
