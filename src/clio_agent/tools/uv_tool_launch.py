"""Launchers that run a server from uv's tool environments: ``uvx`` and ``uv tool run``.

:mod:`clio_agent.tools.mcp_prepare` provisions a server's environment before it starts. For
these launchers it needs the interpreter a ``uv tool install`` console script runs
(:func:`launcher_interpreter`) and, where builds run outside the fence, the argv that builds
a ``uvx`` server's ephemeral environment without starting the server (:func:`uvx_warm_argv`).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path


def _uv_tool_python(launcher: Path) -> Path | None:
    """The interpreter of a ``uv tool install`` console script in uv's tool bin folder."""

    bin_dirs = [os.environ.get("UV_TOOL_BIN_DIR", ""), str(Path.home() / ".local" / "bin")]
    parent = os.path.normcase(str(launcher.parent))
    if not any(value and os.path.normcase(str(Path(value))) == parent for value in bin_dirs):
        return None
    tools = os.environ.get("UV_TOOL_DIR") or ""
    if not tools:
        roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        tools = str(Path(roaming) / "uv" / "tools") if os.name == "nt" else ""
    if not tools:
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        tools = str(Path(data) / "uv" / "tools")
    scripts = "Scripts" if os.name == "nt" else "bin"
    python = Path(tools) / launcher.stem / scripts / ("python.exe" if os.name == "nt" else "python")
    return python if python.is_file() else None


def launcher_interpreter(resolved_command: str) -> str | None:
    """The Python interpreter that runs the ``clio-kit`` console script ``resolved_command``.

    A script in a virtual environment sits beside its interpreter; a ``uv tool``
    script runs its tool environment's interpreter. Anything else is unknown (``None``).
    """

    launcher = Path(resolved_command)
    sibling = launcher.with_name("python.exe" if os.name == "nt" else "python")
    if sibling.is_file() and (launcher.parent.parent / "pyvenv.cfg").is_file():
        return str(sibling)
    tool = _uv_tool_python(launcher)
    return str(tool) if tool is not None else None


def uvx_warm_argv(command: str, args: Sequence[str], resolved: str) -> tuple[str, ...] | None:
    """The argv that builds a ``uvx`` / ``uv tool run`` server's environment without starting it.

    The declared options are kept and the server command is replaced by the environment's
    ``python -c pass``. uv keys the ephemeral environment on the requirements, not on the
    command, so the server's later start reuses it. That start then only verifies a pinned
    git source with ``git rev-parse``, instead of cloning it through Git for Windows' MSYS
    ``sh``, which cannot start inside the Windows fence. ``None`` when ``command`` is
    neither launcher, or when an option this module does not model makes the server
    command impossible to locate safely.
    """

    from clio_agent.tools.launcher_cache_lock import _launcher_name  # noqa: PLC0415
    from clio_agent.tools.uv_launcher import _BOOL_FLAGS, _VALUE_FLAGS, _is_uv  # noqa: PLC0415

    tokens = [str(arg) for arg in args]
    if _launcher_name(command) == "uvx":
        prefix: list[str] = [resolved]
    elif _is_uv(command) and tokens[:2] == ["tool", "run"]:
        prefix, tokens = [resolved, "tool", "run"], tokens[2:]
    else:
        return None
    options: list[str] = []
    has_source = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "-":
            break
        flag, _, inline = token.partition("=")
        if flag == "--from" or flag in _VALUE_FLAGS:
            width = 1 if inline else 2
            if index + width > len(tokens):
                return None
            has_source = has_source or flag == "--from"
            options.extend(tokens[index : index + width])
            index += width
            continue
        if flag in _BOOL_FLAGS and not inline:
            options.append(token)
            index += 1
            continue
        return None
    if index >= len(tokens):
        return None
    if not has_source:
        options.extend(["--from", tokens[index]])
    return (*prefix, *options, "python", "-c", "pass")
