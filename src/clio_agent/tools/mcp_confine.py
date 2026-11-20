"""Confine a stdio MCP server's spawn in the fleet fence (#975).

:func:`clio_agent.tools.mcp_config.transport_for` resolves a server's launcher and
environment; this puts the final argv in the shared confinement owner, gives it the same
route out as a fenced shell, and registers its egress channel for its namespace.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any


def confine_stdio_server(
    name: str, command: str, args: Sequence[str], env: dict[str, str], cwd: str | None
) -> tuple[Any, dict[str, str]]:
    """``(confined, env)``: the fenced spawn of ``command`` and the environment it runs with.

    A fenced server's ssh, scp, sftp and Git over SSH take the same route out as a fenced
    shell's (through the CONNECT helper and the chokepoint's network policy), and its temp
    folder leaves room for a unix socket path. ``cwd`` is preserved independently of grants,
    with the existing ``pdeathsig=False`` lifetime.
    """
    from clio_agent.runtime import sandbox  # noqa: PLC0415 - avoid import cycle
    from clio_agent.runtime.fence_loopback import wrap_publishing  # noqa: PLC0415
    from clio_agent.runtime.fence_ssh import fence_ssh_environment  # noqa: PLC0415
    from clio_agent.runtime.session_scratch import socket_safe_temp  # noqa: PLC0415

    env = {**env, **fence_ssh_environment(env), **socket_safe_temp(env)}
    # Windows' MXC fence drops host connections to the server's loopback listeners, so the
    # server runs behind CLIO's loopback agent, which publishes them (fence_loopback).
    command, args, loopback_env, loopback_token = wrap_publishing(
        command, list(args), sandbox.current_state()
    )
    env.update(loopback_env)
    confined = sandbox.wrap_confined(
        command,
        args,
        write_roots=sandbox.effective_write_roots(sandbox.PROFILE_FLEET, workspace_root=cwd),
        net_policy=sandbox.NET_ALLOW_RECORD,
        profile=sandbox.PROFILE_FLEET,
        pdeathsig=False,
        cwd=cwd if cwd is not None else os.getcwd(),
        egress_root=cwd,
    )
    # B5 #979.7 (the deferred B4 WRITER): this fleet proxy is ONE persistent confined child
    # per (workspace, namespace). Register its ``net_child_id`` (empty on the floor → no-op)
    # keyed by ``name`` (the namespace) + ``cwd`` (the workspace root) so the gact
    # tool-observer can join ``call_id -> serving child`` for the egress ingest mint (#978
    # pt 5). Runtime-layer registry (sandbox_net) so this seam never imports gact.
    net_child = str(confined.result.details.get("net_child_id") or "")
    if loopback_token:
        from clio_agent.runtime.fence_loopback import publisher  # noqa: PLC0415

        publisher().label(loopback_token, net_child)
    if net_child:
        from clio_agent.runtime.sandbox_net import register_namespace_child  # noqa: PLC0415

        register_namespace_child(cwd, name, net_child)
    return confined, env
