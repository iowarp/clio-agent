"""``clio-agent mcp``: connect, list, check and remove your own MCP services.

clio-agent mcp add NAME -- COMMAND [ARG ...]     a local command (stdio)
clio-agent mcp add NAME --url URL                a remote service (streamable HTTP)
    [--env NAME=VALUE ...]                       process settings for a local command
    [--only-when-asked]                          not given to every conversation
clio-agent mcp list [--json]
clio-agent mcp check NAME                        start it and list its tools
clio-agent mcp remove NAME
clio-agent mcp trust [--workspace DIR] [--yes]   mount this workspace's mcp.yaml servers

These are the same saved services as Settings > MCP tools. A running service
picks a change up when its tools are next listed; restart it to be certain.

A workspace's own ``.clio-agent/shared/mcp.yaml`` declares commands CLIO would
start, so its servers are mounted only after ``mcp trust`` records that file's
exact content. ``trust`` shows each server first, and an edited file needs
trusting again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any


def _declaration(args: argparse.Namespace) -> dict[str, Any]:
    if args.url:
        if args.command:
            raise SystemExit("give either --url or a command after --, not both")
        return {"transport": "http", "url": args.url, "always_load": not args.only_when_asked}
    if not args.command:
        raise SystemExit("give a command after --, or --url for a remote service")
    env: dict[str, str] = {}
    for item in args.env or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--env takes NAME=VALUE, not {item!r}")
        env[key] = value
    return {
        "transport": "stdio",
        "command": args.command[0],
        "args": list(args.command[1:]),
        "env": env,
        "always_load": not args.only_when_asked,
    }


def _trust_workspace(args: argparse.Namespace) -> int:
    """Show the workspace mcp.yaml's servers, then trust its current content."""
    from pathlib import Path  # noqa: PLC0415

    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.tools import workspace_mcp_trust as trust  # noqa: PLC0415
    from clio_agent.tools.mcp_config import MCPConfigError, _read_mcp_yaml  # noqa: PLC0415

    path = paths.workspace_config_path(Path(args.workspace or ".").resolve(), "mcp.yaml")
    state = trust.inspect_workspace_mcp(path)
    if state is None:
        print(f"no workspace mcp.yaml at {path}")
        return 1
    try:
        declared = _read_mcp_yaml(path)
    except MCPConfigError as exc:
        print(f"error: {exc}")
        return 2
    print(f"{path}\nsha256 {state.digest}{' (already trusted)' if state.trusted else ''}")
    for name, value in declared.items():
        if isinstance(value, dict):
            target = value.get("url") or " ".join(
                [str(value.get("command") or ""), *map(str, value.get("args") or [])]
            )
            if isinstance(value.get("env"), dict) and value["env"]:
                target += f"  (env: {', '.join(map(str, value['env']))})"
        else:
            target = str(value)
        print(f"  {name}: {target}")
    if state.trusted:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("not trusted: review the servers above, then run again with --yes")
            return 1
        if input("Start these commands for this workspace? [y/N] ").strip().lower() != "y":
            print("not trusted")
            return 1
    try:
        trust.trust_workspace_mcp(path, expected_digest=state.digest)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print("trusted; its servers load in the next conversation in this workspace")
    return 0


def run_mcp_cli(arguments: Sequence[str]) -> int:
    from clio_agent.env_file import load_project_env_file  # noqa: PLC0415
    from clio_agent.gact import mcp_user_configuration as saved  # noqa: PLC0415
    from clio_agent.tools.mcp_redaction import redact_mcp_spec  # noqa: PLC0415

    load_project_env_file()
    parser = argparse.ArgumentParser(prog="clio-agent mcp")
    sub = parser.add_subparsers(dest="action", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("--url", default="")
    p_add.add_argument("--env", action="append")
    p_add.add_argument("--only-when-asked", action="store_true")
    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")
    sub.add_parser("check").add_argument("name")
    sub.add_parser("remove").add_argument("name")
    p_trust = sub.add_parser("trust")
    p_trust.add_argument("--workspace", default="")
    p_trust.add_argument("--yes", action="store_true")
    # Everything after ``--`` is the service's own command line, never parsed here.
    arguments = list(arguments)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:split])
    args.command = arguments[split + 1 :]

    if args.action == "trust":
        return _trust_workspace(args)
    try:
        if args.action == "add":
            declaration = saved.set_user_mcp_server(args.name, _declaration(args))
            reach = "every conversation" if declaration["always_load"] else "when asked"
            print(f"saved {args.name} ({declaration['transport']}), available in {reach}")
            print(f"check it: clio-agent mcp check {args.name}")
            return 0
        if args.action == "list":
            rows = {
                name: redact_mcp_spec(spec) for name, spec in saved.list_user_mcp_servers().items()
            }
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for name, spec in rows.items():
                    target = spec.get("url") or " ".join(
                        [str(spec.get("command") or ""), *map(str, spec.get("args") or [])]
                    )
                    print(f"{name}  {spec.get('transport', 'stdio')}  {target}")
                if not rows:
                    print("no MCP services are saved")
            return 0
        if args.action == "check":
            found = saved.get_user_mcp_server(args.name)
            if found is None:
                print(f"{args.name!r} is not saved")
                return 1
            from clio_agent.gact.routes.mcp_configuration import (  # noqa: PLC0415
                _probe_user_mcp_server,
            )

            tools, error = asyncio.run(_probe_user_mcp_server(found))
            if error:
                print(f"{args.name}: not ready: {error}")
                return 1
            print(f"{args.name}: ready, {len(tools)} tool(s): {', '.join(tools)}")
            return 0
        removed = saved.remove_user_mcp_server(args.name)
        print(f"removed {args.name}" if removed else f"{args.name!r} is not saved")
        return 0 if removed else 1
    except saved.McpUserConfigurationError as exc:
        print(f"error: {exc}")
        return 2


__all__ = ["run_mcp_cli"]
