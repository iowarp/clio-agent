"""Start a provisioned ``uv run --project`` server from its environment, not through ``uv``.

CLIO builds a ``uv run --project <dir>`` server's environment before the server's first
start (:func:`clio_agent.tools.mcp_prepare.provision`) and records the build with a
marker. ``uv run`` still re-checks the project at every spawn, and often syncs it again
(resolving the lock, reinstalling the project, compiling bytecode) before the server's
first line: seconds on a warm machine and tens of seconds on a loaded one, every start.
On Windows a second spawn syncing an environment the first is running from can also fail.

Once the marker matches the declaration the environment is complete for it, so the
server's console script is started directly, with what ``uv run`` gives it:
``VIRTUAL_ENV`` and the environment's scripts directory first on ``PATH``. Nothing is
installed or resolved at spawn.

Only the plain shape is taken over: ``--project`` (``--directory`` also changes the
working directory) with flags that only choose what is synced. Any other flag, a
``--no-sync`` launcher, a missing marker or a missing console script keeps the declared
``uv run``, with a typed trace reason. ``tools.mcp.provisioned_direct`` (true) turns it off.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clio_agent.runtime import trace

__all__ = ["direct_launch_enabled", "provisioned_launch"]

#: ``uv run`` flags that only choose what the provisioned environment holds. The
#: provisioning fingerprint covers them, so a matching marker means they were applied.
_SYNC_ONLY_FLAGS = frozenset(
    {
        "--project",
        "--frozen",
        "--locked",
        "--dev",
        "--no-dev",
        "--only-dev",
        "--extra",
        "--all-extras",
        "--group",
        "--no-group",
        "--only-group",
        "--all-groups",
        "--no-default-groups",
        "--package",
        "--python",
        "-p",
        "--no-editable",
        "--offline",
        "--quiet",
        "-q",
        "--no-progress",
        "--compile-bytecode",
    }
)


def direct_launch_enabled() -> bool:
    """``tools.mcp.provisioned_direct`` (true): start provisioned servers without ``uv``."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "tools.mcp.provisioned_direct",
        env="CLIO_MCP_PROVISIONED_DIRECT",
        default=True,
        cast=conf.as_bool,
    )


def _server_args(args: Sequence[str]) -> tuple[str, list[str]] | str:
    """``(entry point, its arguments)`` for a sync-only ``uv run`` argv, else a reason."""

    from clio_agent.tools.uv_launcher import _VALUE_FLAGS  # noqa: PLC0415

    tokens = [str(arg) for arg in args]
    index = 1  # past "run"
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            if index + 1 >= len(tokens):
                return "no_entrypoint"
            return tokens[index + 1], tokens[index + 2 :]
        if not token.startswith("-") or token == "-":
            return token, tokens[index + 1 :]
        flag, _, inline = token.partition("=")
        if flag not in _SYNC_ONLY_FLAGS:
            return f"flag_not_sync_only:{flag}"
        index += 2 if flag in _VALUE_FLAGS and not inline else 1
    return "no_entrypoint"


def _console_script(environment: Path, entrypoint: str) -> Path | None:
    suffixes = (".exe", "") if os.name == "nt" else ("",)
    for scripts in ("Scripts", "bin"):
        for suffix in suffixes:
            candidate = environment / scripts / f"{entrypoint}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _with_environment(env: Mapping[str, str], environment: Path, scripts: Path) -> dict[str, str]:
    launched = dict(env)
    path_key = next((key for key in launched if key.upper() == "PATH"), "PATH")
    current = launched.get(path_key, "")
    launched[path_key] = f"{scripts}{os.pathsep}{current}" if current else str(scripts)
    launched["VIRTUAL_ENV"] = str(environment)
    launched.pop("PYTHONHOME", None)
    return launched


def provisioned_launch(
    spec: Any, resolved_command: str, args: Sequence[str], env: Mapping[str, str]
) -> tuple[str, list[str], dict[str, str]] | None:
    """``(command, args, env)`` starting ``spec``'s provisioned server directly, else ``None``.

    ``None`` keeps the declared launch: not a ``uv run --project`` server, a flag this
    does not take over, or an environment not provisioned for this declaration.
    """

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415
    from clio_agent.tools.uv_launcher import parse_uv_run_launcher  # noqa: PLC0415

    command = str(getattr(spec, "command", ""))
    launcher = parse_uv_run_launcher(command, [str(arg) for arg in args])
    if launcher is None or launcher.sync_disabled:
        return None
    name = str(getattr(spec, "name", ""))

    def keep(reason: str) -> None:
        trace.event("TOOLS", "mcp_spawn_provisioned_declared namespace=%s reason=%s", name, reason)

    if not direct_launch_enabled():
        keep("disabled")
        return None
    parsed = _server_args(args)
    if isinstance(parsed, str):
        keep(parsed)
        return None
    entrypoint, server_args = parsed
    plan = mcp_prepare.provision_plan(spec, resolved_command)
    if isinstance(plan, str) or not plan.marker_dir:
        keep("no_provision_plan")
        return None
    if not mcp_prepare._marker_matches(plan):
        keep("not_provisioned")
        return None
    environment = Path(plan.marker_dir)
    script = _console_script(environment, entrypoint)
    if script is None:
        keep("console_script_missing")
        return None
    trace.event(
        "TOOLS",
        "mcp_spawn_provisioned_direct namespace=%s entrypoint=%s environment=%s",
        name,
        entrypoint,
        environment,
    )
    return str(script), server_args, _with_environment(env, environment, script.parent)
