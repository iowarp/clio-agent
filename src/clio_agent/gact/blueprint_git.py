"""Git for blueprint installs: waited for while git works, typed when it stalls.

:func:`run_git` answers fast when git does, keeps waiting while git's process tree is working (CPU / I/O, 180 s ceiling), and raises
:class:`BlueprintGitStalledError` -- a ``ValueError``, so the install/update routes
already turn it into a typed 400 -- when it stops making progress.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

from clio_agent.runtime.progress import ProbeUnresponsiveError, run_probe

#: A git step answering within this costs nothing extra; past it, each stretch this long
#: must show git working.
GIT_FIRST_WAIT_S = 20.0


class BlueprintGitStalledError(ValueError):
    """A git step of a blueprint install stopped making progress (slow or unreachable remote)."""

    def __init__(self, cmd: Sequence[str], exc: ProbeUnresponsiveError) -> None:
        self.reason = f"blueprint_git_{exc.reason}"
        super().__init__(
            f"{' '.join(cmd[:2])} made no progress after {exc.waited_s:.0f}s ({self.reason}); "
            "the remote is slow or unreachable"
        )


def run_git(
    cmd: Sequence[str], *, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one git step; ``CalledProcessError`` on a non-zero exit, like ``check=True``.

    Raises:
        BlueprintGitStalledError: git stopped working before it finished.
        subprocess.CalledProcessError: git exited non-zero.
        OSError: git could not be launched.
    """
    try:
        result = run_probe(list(cmd), op=" ".join(cmd[:2]), first_wait_s=GIT_FIRST_WAIT_S, env=env)
    except ProbeUnresponsiveError as exc:
        raise BlueprintGitStalledError(cmd, exc) from exc
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, list(cmd), result.stdout, result.stderr
        )
    return result


__all__ = ["GIT_FIRST_WAIT_S", "BlueprintGitStalledError", "run_git"]
