"""Drive the Claude Code CLI's own sign-in (``claude auth login``) from CLIO.

Daisy asked to sign in to Claude from inside CLIO (#1454). The CLI already has
a flow a UI can drive, verified on Claude Code 2.1.276/2.1.281:

* ``claude auth login`` prints ``If the browser didn't open, visit: <url>``
  (an OAuth authorize URL whose redirect shows the person a code), then
  ``Paste code here if prompted >`` and reads that code from stdin;
* on success it exits 0 and ``claude auth status`` reports ``loggedIn: true``;
  a bad code exits non-zero with its own reason
  (``Login failed: Request failed with status code 400``).

CLIO only relays: the URL to the person, the pasted code to the CLI's stdin,
the CLI's own outcome back. It never reads or writes Claude's credential files;
the CLI writes them where the Agent SDK (same binary, same environment) reads
them. The binary is the one the SDK runs (``_resolve_claude_binary``).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field

__all__ = ["ClaudeLoginError", "ClaudeLoginFlow", "drop_flow", "get_flow", "start_login"]

#: The authorize URL the CLI prints, once complete (OSC 8 escapes are stripped first).
_URL_RE = re.compile(r"https://\S+/oauth/authorize\?\S+(?=\s)")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_FLOWS: dict[str, ClaudeLoginFlow] = {}
_FLOWS_LOCK = threading.Lock()


class ClaudeLoginError(RuntimeError):
    """The CLI could not start a sign-in (not installed, or printed no URL)."""


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", _OSC_RE.sub("", text))


@dataclass
class ClaudeLoginFlow:
    """One running ``claude auth login`` process and what it has said so far."""

    flow_id: str
    process: subprocess.Popen[str]
    output: list[str] = field(default_factory=list)
    url: str = ""
    url_seen: threading.Event = field(default_factory=threading.Event)
    exited: threading.Event = field(default_factory=threading.Event)
    code_submitted: bool = False
    reader: threading.Thread | None = None

    def _read(self) -> None:
        stream = self.process.stdout
        if stream is not None:
            buffer = ""
            while chunk := stream.read(1):
                buffer += chunk
                if not self.url:
                    match = _URL_RE.search(_clean(buffer))
                    if match:
                        self.url = match.group(0)
                        self.url_seen.set()
                if chunk == "\n" or buffer.endswith("> "):
                    self.output.append(_clean(buffer).strip())
                    buffer = ""
            if buffer.strip():
                self.output.append(_clean(buffer).strip())

    def _wait(self) -> None:
        # Exit is the process's own; then drain its last words (the reader ends
        # when every writer of the pipe, helpers included, has exited).
        self.process.wait()
        if self.reader is not None:
            self.reader.join()
        self.exited.set()
        self.url_seen.set()  # wake a waiter even when no URL was printed

    def submit_code(self, code: str) -> None:
        """Hand the person's pasted code to the CLI (its own prompt reads it)."""

        stdin = self.process.stdin
        if stdin is None or self.exited.is_set():
            raise ClaudeLoginError("This sign-in attempt has ended. Start sign-in again.")
        stdin.write(code.strip() + "\n")
        stdin.flush()
        stdin.close()
        self.code_submitted = True

    def cli_reason(self) -> str:
        """The CLI's own last words (e.g. ``Login failed: ...``)."""

        lines = [line for line in self.output if line and "Paste code here" not in line]
        return lines[-1] if lines else ""

    def state(self) -> tuple[str, str]:
        """``(pending|exited_ok|failed, reason)`` from the process alone."""

        if not self.exited.is_set():
            return "pending", ""
        if self.process.returncode == 0:
            return "exited_ok", ""
        return "failed", self.cli_reason() or f"claude auth login exited {self.process.returncode}"

    def cancel(self) -> None:
        """Stop the CLI and anything it spawned."""

        if self.exited.is_set():
            return
        if os.name == "nt":
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            self.process.kill()


def start_login(binary: str, *, wait_s: float) -> ClaudeLoginFlow:
    """Start ``<binary> auth login`` and return once it printed its sign-in URL.

    Any earlier flow is cancelled first: the person asked to sign in again.

    Args:
        binary: The Claude Code binary the Agent SDK runs.
        wait_s: How long to wait for the URL (the configured Claude Code CLI
            check timeout, never a compiled-in constant).

    Raises:
        ClaudeLoginError: The CLI could not be started or printed no URL.
    """

    with _FLOWS_LOCK:
        for old in _FLOWS.values():
            old.cancel()
        _FLOWS.clear()
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = {**os.environ, "BROWSER": "none"}  # the person's browser opens the URL, not the host
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [binary, "auth", "login", "--claudeai"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **kwargs,  # type: ignore[arg-type]
        )
    except OSError as exc:
        raise ClaudeLoginError(f"Claude Code could not start its sign-in: {exc}") from exc
    flow = ClaudeLoginFlow(flow_id=uuid.uuid4().hex, process=process)
    flow.reader = threading.Thread(target=flow._read, name="claude-auth-login-read", daemon=True)
    flow.reader.start()
    threading.Thread(target=flow._wait, name="claude-auth-login-wait", daemon=True).start()
    flow.url_seen.wait(wait_s)
    if not flow.url:
        flow.cancel()
        reason = flow.cli_reason() or "Claude Code printed no sign-in address."
        raise ClaudeLoginError(f"Claude Code could not start its sign-in: {reason}")
    with _FLOWS_LOCK:
        _FLOWS[flow.flow_id] = flow
    return flow


def get_flow(flow_id: str) -> ClaudeLoginFlow | None:
    """The running flow ``flow_id``, or ``None`` when it ended or never existed."""

    with _FLOWS_LOCK:
        return _FLOWS.get(flow_id)


def drop_flow(flow_id: str) -> None:
    """Forget (and stop) a finished flow."""

    with _FLOWS_LOCK:
        flow = _FLOWS.pop(flow_id, None)
    if flow is not None:
        flow.cancel()
