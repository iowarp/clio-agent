"""Run a native document tool on untrusted bytes inside the OS write fence, with no network."""

from __future__ import annotations

import os
from pathlib import Path

from clio_agent.runtime.document_stack.process import run

__all__ = ["run_confined"]


def run_confined(
    command: list[str],
    *,
    cwd: Path,
    workspace_root: Path | None,
    timeout_seconds: float = 120.0,
    office: bool = False,
) -> str:
    """Run ``command`` confined to the workspace's write roots with the network denied.

    With no ``workspace_root`` the command runs unconfined, as the rendition pipeline does
    outside a workspace. ``office`` marks a LibreOffice command (see
    :func:`sandbox.wrap_confined`). Returns the command's standard output.

    Raises:
        DocumentError: the command failed or timed out.
        SandboxCompositionError: the write fence could not be composed.
    """

    env = None
    confined = None
    if workspace_root is not None:
        from clio_agent.runtime import sandbox  # noqa: PLC0415 - heavy import on demand

        confined = sandbox.wrap_confined(
            command[0],
            command[1:],
            write_roots=sandbox.effective_write_roots(
                sandbox.PROFILE_SHELL, workspace_root=str(workspace_root)
            ),
            net_policy=sandbox.NET_DENY,
            profile=sandbox.PROFILE_SHELL,
            pdeathsig=False,
            office=office,
        )
        command = [confined.command, *confined.args]
        env = {**os.environ, **confined.env_overlay}
    try:
        return run(command, cwd=cwd, timeout=timeout_seconds, env=env)
    finally:
        if confined is not None:
            confined.release()
