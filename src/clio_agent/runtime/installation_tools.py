"""Put a scoped installation's own ``clio-kit`` on this process's PATH.

A CLIO installed with its own prefix, home, data or user folder keeps clio-kit in
``<prefix>/uv-tools`` (install.sh, install.ps1) so it never replaces the user's own.
The CLI launchers (``install/clio``, ``install/clio.ps1``) put that folder on the
agent's PATH; the desktop starts the agent through its own launcher and does not.
Without it, every plugin MCP server launched through clio-kit fails to spawn
(``mcp_launcher:clio-kit`` degraded). The agent therefore adopts the folder itself at
start-up, for every host: desktop, CLI and service.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from pathlib import Path

__all__ = ["adopt_installation_tools", "installation_prefix"]

_LAUNCHER = "clio-kit"


def installation_prefix(prefix: str | None = None) -> Path | None:
    """The install prefix whose ``clio-agent/.venv`` runs this interpreter, else ``None``."""

    venv = Path(sys.prefix if prefix is None else prefix)
    if venv.name != ".venv" or venv.parent.name != "clio-agent":
        return None
    return venv.parent.parent


def adopt_installation_tools(
    environ: MutableMapping[str, str] | None = None, *, prefix: Path | None = None
) -> Path | None:
    """Prepend ``<prefix>/uv-tools/bin`` to PATH when it holds this installation's clio-kit.

    Also points ``UV_TOOL_DIR``/``UV_TOOL_BIN_DIR`` at it, as the CLI launchers do, so
    the MCP preparation step finds the launcher's interpreter (``tools/mcp_prepare.py``).

    Returns:
        The bin folder adopted, or ``None`` when this installation has no own clio-kit.
    """

    env = os.environ if environ is None else environ
    root = installation_prefix() if prefix is None else prefix
    if root is None:
        return None
    tools = root / "uv-tools"
    bin_dir = tools / "bin"
    launcher = bin_dir / (f"{_LAUNCHER}.exe" if os.name == "nt" else _LAUNCHER)
    if not launcher.is_file():
        return None
    env["UV_TOOL_DIR"] = str(tools)
    env["UV_TOOL_BIN_DIR"] = str(bin_dir)
    wanted = os.path.normcase(os.path.normpath(str(bin_dir)))
    rest = [
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part and os.path.normcase(os.path.normpath(part)) != wanted
    ]
    env["PATH"] = os.pathsep.join([str(bin_dir), *rest])
    return bin_dir
