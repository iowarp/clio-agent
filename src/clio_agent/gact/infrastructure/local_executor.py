"""Run a server-generated command on the controller host, streaming its output.

The result keeps only the last :data:`MAX_OPERATION_LOG_CHARS` of each stream
(what callers parse and store), but every chunk is handed to ``on_output`` as
it arrives, so an operation's live log never depends on that tail. Cancelling
the awaiting task stops the command's whole process group (a detached worker
in its own session is not touched) and leaves whatever it wrote -- a layer
cache, a partial download -- in place.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import threading
from collections.abc import Callable
from typing import IO
from urllib.parse import urlsplit

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec

MAX_OPERATION_LOG_CHARS = 16_000
#: Seconds a stopped command gets after SIGTERM before SIGKILL.
STOP_GRACE_SECONDS = 5.0

#: ``on_output(text, stream)`` with ``stream`` ``"stdout"`` or ``"stderr"``.
OutputSink = Callable[[str, str], None]


def bounded(value: str) -> str:
    """The last :data:`MAX_OPERATION_LOG_CHARS` of ``value`` (marked when cut)."""

    if len(value) <= MAX_OPERATION_LOG_CHARS:
        return value
    return f"…{value[-MAX_OPERATION_LOG_CHARS:]}"


def tcp_reachable(url: str) -> bool:
    """Whether a TCP connection to ``url``'s host and port opens within 2 s."""

    parsed = urlsplit(url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=2):
            return True
    except OSError:
        return False


class _Tail:
    """A thread-safe, bounded tail of one output stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ""
        self.cut = False

    def add(self, chunk: str) -> None:
        with self._lock:
            self._text += chunk
            if len(self._text) > 2 * MAX_OPERATION_LOG_CHARS:
                self._text = self._text[-MAX_OPERATION_LOG_CHARS:]
                self.cut = True

    def value(self) -> str:
        with self._lock:
            text = self._text
        return f"…{text[-MAX_OPERATION_LOG_CHARS:]}" if self.cut else bounded(text)


def _stop(process: subprocess.Popen[str]) -> None:
    """Stop the command's process group (POSIX) or the process (Windows)."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        process.kill()
        return
    for sig, grace in ((signal.SIGTERM, STOP_GRACE_SECONDS), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


async def run_local(spec: CommandSpec, on_output: OutputSink | None = None) -> CommandResult:
    """Run ``spec`` locally; stream chunks to ``on_output`` on the event loop."""

    loop = asyncio.get_running_loop()
    try:
        process = subprocess.Popen(
            [spec.program, *spec.args],
            stdin=subprocess.PIPE if spec.stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        return CommandResult(exit_code=127, stderr=f"{spec.program} is not installed: {exc}")
    tails = {"stdout": _Tail(), "stderr": _Tail()}

    def pump(stream: IO[str], name: str) -> None:
        for chunk in iter(stream.readline, ""):
            tails[name].add(chunk)
            if on_output is not None and not loop.is_closed():
                loop.call_soon_threadsafe(on_output, chunk, name)
        stream.close()

    def feed(stream: IO[str]) -> None:
        try:
            stream.write(spec.stdin)
            stream.close()
        except (BrokenPipeError, OSError):
            return

    readers = [
        threading.Thread(target=pump, args=(stream, name), daemon=True)
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr"))
    ]
    for reader in readers:
        reader.start()
    if spec.stdin and process.stdin is not None:
        threading.Thread(target=feed, args=(process.stdin,), daemon=True).start()
    try:
        exit_code = await asyncio.wait_for(
            loop.run_in_executor(None, process.wait), timeout=spec.timeout_seconds
        )
    except TimeoutError:
        await loop.run_in_executor(None, _stop, process)
        return CommandResult(
            exit_code=124,
            stdout=tails["stdout"].value(),
            stderr=f"Command timed out after {spec.timeout_seconds:g} seconds",
        )
    except asyncio.CancelledError:
        # The person cancelled: stop what this command started, keep its files.
        await asyncio.shield(loop.run_in_executor(None, _stop, process))
        raise
    for reader in readers:
        # A daemon the command left behind may hold the pipe open; do not wait on it.
        await loop.run_in_executor(None, reader.join, 5.0)
    # Deliver the readers' last chunks before the caller reads the result.
    await asyncio.sleep(0)
    return CommandResult(
        exit_code=exit_code, stdout=tails["stdout"].value(), stderr=tails["stderr"].value()
    )


__all__ = ["MAX_OPERATION_LOG_CHARS", "OutputSink", "bounded", "run_local", "tcp_reachable"]
