"""Classify why an installed container runtime could not answer its health check.

A runtime that is installed but unusable prints a diagnostic that only an
engineer can read (``error during connect: Get "http://%2F%2F.%2Fpipe%2F
dockerDesktopLinuxEngine/v1.51/info": open //./pipe/...: The system cannot find
the file specified``). The person needs the fact underneath it: the engine is
not running, or their account may not use it. This module turns the runtime's
own output into one typed :data:`RuntimeFailure` the UI renders as plain text;
the raw line stays on :attr:`ContainerRuntimeFact.detail` for a details view.

This reads *reality* -- the runtime's own error text and exit -- not a model's
prose, and it never changes what the probe decided: the runtime is still
unusable, the classification only says why. Anything unrecognized is
``unknown``, never guessed into a friendlier bucket.
"""

from __future__ import annotations

from clio_agent.gact.infrastructure.models import RuntimeFailure, RuntimeName

#: Exit status GNU ``timeout`` uses when it killed the command (remote probe).
TIMEOUT_EXIT_STATUS = 124

# Fragments each runtime prints when its engine/daemon/VM is not reachable.
# Docker CLI (Linux socket, Docker Desktop named pipe on Windows, macOS
# socket); Podman remote clients (Windows/macOS podman machine).
_NOT_RUNNING = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "docker desktop is not running",
    "docker desktop is manually paused",
    "cannot connect to podman",
    "unable to connect to podman",
    "podman machine start",
    # The engine's socket/pipe is absent: ``dial unix /var/run/docker.sock:
    # connect: no such file or directory`` / Windows named pipe not found. A
    # bare "no such file" is NOT enough (rootless Podman's missing runtime
    # directory is a setup problem, not a stopped engine).
    "connect: no such file or directory",
    "the system cannot find the file specified",
    "connect: connection refused",
)

_PERMISSION_DENIED = (
    "permission denied",
    "access is denied",
    "operation not permitted",
)


def classify_runtime_failure(
    name: RuntimeName, detail: str, *, timed_out: bool = False
) -> RuntimeFailure:
    """Say why an installed runtime failed its health command.

    Args:
        name: The runtime that failed (reserved for runtime-specific rules;
            the fragments above are already runtime-scoped by their wording).
        detail: The runtime's own last output line, or the exception text.
        timed_out: The health command was killed for exceeding its bound.

    Returns:
        ``permission_denied`` when the account may not use the engine,
        ``not_running`` when the engine is not reachable, ``timed_out`` when
        it never answered, else ``unknown``.
    """

    del name
    if timed_out:
        return "timed_out"
    text = detail.casefold()
    # Checked first: Docker's permission error also says "connect to the
    # Docker daemon socket", and the fix (group membership) is different.
    if any(fragment in text for fragment in _PERMISSION_DENIED):
        return "permission_denied"
    if any(fragment in text for fragment in _NOT_RUNNING):
        return "not_running"
    return "unknown"


__all__ = ["TIMEOUT_EXIT_STATUS", "classify_runtime_failure"]
