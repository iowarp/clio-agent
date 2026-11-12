"""Run one Codex qualification probe and keep what Codex said about it.

The Windows qualification probes (native MXC in :mod:`sandbox_codex_mxc`, the
legacy elevated fence in :mod:`sandbox_codex`) and the Linux bubblewrap probe
(:func:`probe_codex_linux_fence`) launch ``codex sandbox`` and judge it by file
witnesses. This module owns the launch, so a failure keeps Codex's own
explanation instead of a bare exit code:

- stderr goes to an anonymous temporary file, never a pipe: Codex hands handles to
  background helpers, and a pipe would keep the wait open after the command ends;
- a timeout kills the whole process tree before the caller's temporary folders are
  removed, so the timeout stays the reported failure instead of a cleanup error.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

#: The most of Codex's stderr kept for a receipt, a log line or a message.
STDERR_TAIL_CHARS = 1500
#: What a person does after a failed qualification, in Desktop or in a terminal.
RETRY_SETUP_ACTION = (
    "Retry Set up protected execution (Infrastructure > CLIO hosts > Protected execution), "
    "or run `clio sandbox setup`."
)


@dataclass(frozen=True)
class ProbeRun:
    """What one probe launch did: its exit code, or that it ran out of time."""

    returncode: int | None
    timed_out: bool
    stderr: str

    def describe(self) -> str:
        """One line for a log or a message: how it ended, then what Codex printed."""
        ending = "timed out" if self.timed_out else f"exited {self.returncode}"
        return f"codex {ending}: {self.stderr}" if self.stderr else f"codex {ending}"


class EnforcementVerdict(tuple[bool, str]):
    """``(enforced, reason)`` that also carries the probe's ``detail`` for the message.

    It unpacks like the plain pair, so injected verifiers returning a tuple still fit.
    """

    detail: str

    def __new__(cls, enforced: bool, reason: str, detail: str = "") -> EnforcementVerdict:
        verdict = super().__new__(cls, (enforced, reason))
        verdict.detail = detail
        return verdict


def run_probe(argv: Sequence[str], *, timeout: float, creationflags: int = 0) -> ProbeRun:
    """Run ``argv`` with no stdout, stderr captured to a file, and a hard deadline."""
    with tempfile.TemporaryFile("w+b") as stderr:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            creationflags=creationflags,
        )
        try:
            returncode: int | None = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            process.wait()
            returncode, timed_out = None, True
        stderr.seek(0)
        return ProbeRun(returncode, timed_out, _tail(stderr.read()))


def kill_tree(pid: int) -> None:
    """Kill a probe and every descendant it started; ones already gone are skipped."""
    try:
        root = psutil.Process(pid)
        processes = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(processes, timeout=5)


#: Codex is installed on Linux but its bubblewrap fence cannot start a confined child here
#: (typically: unprivileged user namespaces are disabled or blocked by seccomp/AppArmor).
REASON_CODEX_LINUX_FENCE_UNAVAILABLE = "codex_linux_fence_unavailable"
_LINUX_FENCE_PROBE_TIMEOUT_S = 20


def probe_codex_linux_fence(binary: str) -> EnforcementVerdict:
    """Prove Codex can launch a confined child on this Linux host (never raises).

    Codex wraps children in bubblewrap, which needs a new user namespace. Containers with
    the default seccomp profile and hosts that restrict unprivileged user namespaces refuse
    that, and every wrapped tool would then fail at spawn. The probe runs ``/bin/sh`` through
    the same composition real spawns use and requires it to write a witness inside its root.
    """
    from .sandbox_codex import compose_codex_spawn  # noqa: PLC0415 - sandbox_codex imports this

    try:
        with tempfile.TemporaryDirectory(
            prefix="clio-agent-codex-probe-", ignore_cleanup_errors=True
        ) as allow:
            command, args = compose_codex_spawn(
                [allow], "/bin/sh", ["-c", ": > started"], binary=binary, platform="linux"
            )
            run = run_probe([command, *args], timeout=_LINUX_FENCE_PROBE_TIMEOUT_S)
            if run.returncode == 0 and (Path(allow) / "started").is_file():
                return EnforcementVerdict(True, "codex_linux_fence_verified")
            return EnforcementVerdict(False, REASON_CODEX_LINUX_FENCE_UNAVAILABLE, run.describe())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return EnforcementVerdict(
            False, REASON_CODEX_LINUX_FENCE_UNAVAILABLE, f"the check could not run: {exc!r}"
        )


def mxc_summary(mxc: dict[str, Any]) -> str:
    """The native MXC outcome as a sentence for a failure message ('' when not tried)."""
    if not mxc.get("reason"):
        return ""
    detail = f": {mxc['detail']}" if mxc.get("detail") else ""
    return f"Native MXC ({mxc['reason']}){detail}. "


def _tail(raw: bytes) -> str:
    text = " ".join(raw.decode("utf-8", errors="replace").split())
    return text if len(text) <= STDERR_TAIL_CHARS else "..." + text[-STDERR_TAIL_CHARS:]


__all__ = [
    "REASON_CODEX_LINUX_FENCE_UNAVAILABLE",
    "RETRY_SETUP_ACTION",
    "STDERR_TAIL_CHARS",
    "EnforcementVerdict",
    "ProbeRun",
    "kill_tree",
    "mxc_summary",
    "probe_codex_linux_fence",
    "run_probe",
]
