"""``clio-agent mcp``: connect, list, check and remove your own MCP services.

clio-agent mcp add NAME -- COMMAND [ARG ...]     a local command (stdio)
clio-agent mcp add NAME --url URL                a remote service (streamable HTTP)
    [--env NAME=VALUE ...]                       process settings for a local command
    [--only-when-asked]                          not given to every conversation
clio-agent mcp list [--json]
clio-agent mcp check NAME                        start it and list its tools
clio-agent mcp remove NAME

These are the same saved services as Settings > MCP tools. A running service
picks a change up when its tools are next listed; restart it to be certain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


def run_mcp_cli(arguments: Sequence[str]) -> int:
    from clio_agent.config import load_project_env_file  # noqa: PLC0415
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
    # Everything after ``--`` is the service's own command line, never parsed here.
    arguments = list(arguments)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:split])
    args.command = arguments[split + 1 :]

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
            spec = saved.get_user_mcp_server(args.name)
            if spec is None:
                print(f"{args.name!r} is not saved")
                return 1
            from clio_agent.gact.routes.mcp_configuration import (  # noqa: PLC0415
                _probe_user_mcp_server,
            )

            tools, error = asyncio.run(_probe_user_mcp_server(spec))
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
