"""Remote targets run CLIO's ``python3`` and ``uv`` from the CLIO installed there (owner module).

CLIO brings its own interpreter and uv, so a host needs neither: a minimal server
image has no ``python3``, and an HPC node's may be far too old. Commands say plain
``python3`` / ``uv``; on this computer ``local_executor.local_environment`` puts
CLIO's own first on ``PATH``. For a POSIX SSH target, :func:`on_clio_tools` does the
same for the CLIO deployed in the target's install root (or the default root),
and falls back to the host's only when no CLIO is installed there yet.
"""

from __future__ import annotations

import shlex

from clio_agent.gact.infrastructure.models import CommandSpec, InfrastructureTarget

#: The install root a deployment uses when the target names none (``clio_agent_deploy._ROOT``).
_DEFAULT_ROOTS = (
    '"${XDG_DATA_HOME:-$HOME/.local/share}/clio-agent/app"',
    '"$HOME/Library/Application Support/clio-agent/data/app"',
)
_TOOLS = {
    "python3": "clio-agent/.venv/bin/python",
    "uv": "bin/uv",
}


def on_clio_tools(target: InfrastructureTarget, spec: CommandSpec) -> CommandSpec:
    """``spec`` with ``python3`` / ``uv`` resolved to the target's own CLIO first."""

    relative = _TOOLS.get(spec.program)
    if relative is None or (target.ssh is not None and target.ssh.platform == "windows"):
        return spec
    roots = [shlex.quote(target.install_root.strip())] if target.install_root.strip() else []
    roots += _DEFAULT_ROOTS
    script = (
        f'tool="$1"; shift; for root in {" ".join(roots)}; do '
        f'[ -x "$root/{relative}" ] && exec "$root/{relative}" "$@"; done; exec "$tool" "$@"'
    )
    return spec.model_copy(
        update={"program": "sh", "args": ["-c", script, "clio", spec.program, *spec.args]}
    )
