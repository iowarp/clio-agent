"""``clio-agent plugin``: check, install, trust, update and inspect plugins.

clio-agent plugin check PATH              run the install resolver on a folder
clio-agent plugin check --installed NAME  ... on an installed marketplace
clio-agent plugin install SOURCE [--ref R] [--commit C] [--name N]
clio-agent plugin trust NAME              the explicit trust step
clio-agent plugin update NAME
clio-agent plugin uninstall NAME
clio-agent plugin list                    marketplaces and their plugins
clio-agent plugin agents                  installed agents, user agents included
clio-agent plugin resolve AGENT [--add P] [--remove P] [--prompt]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from clio_agent.plugins.resolve import Delta, ResolutionError


def _print(data: Any, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if as_json else text)


def run_plugin_cli(arguments: Sequence[str]) -> int:
    from clio_agent.config import load_project_env_file  # noqa: PLC0415
    from clio_agent.plugins.check import check  # noqa: PLC0415
    from clio_agent.plugins.service import AgentNotFound, PluginService  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    load_project_env_file()
    parser = argparse.ArgumentParser(prog="clio-agent plugin")
    sub = parser.add_subparsers(dest="action", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("path", nargs="?")
    p_check.add_argument("--installed", default="")
    p_install = sub.add_parser("install")
    p_install.add_argument("source")
    p_install.add_argument("--ref", default="")
    p_install.add_argument("--commit", default="")
    p_install.add_argument("--name", default="")
    for name in ("trust", "update", "uninstall"):
        sub.add_parser(name).add_argument("name")
    sub.add_parser("list")
    sub.add_parser("agents")
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("agent", nargs="?", default="")
    p_resolve.add_argument("--add", action="append", default=[])
    p_resolve.add_argument("--remove", action="append", default=[])
    p_resolve.add_argument("--prompt", action="store_true")
    for p in sub.choices.values():
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(list(arguments))
    service = PluginService()
    try:
        if args.action == "check":
            if not args.path and not args.installed:
                parser.error("check needs PATH or --installed NAME")
            if args.installed:
                service.ensure_default_marketplace()
            report = check(
                Path(args.path) if args.path else None, installed=args.installed, service=service
            )
            _print(report.to_dict(), args.json, report.render())
            return 0 if report.ok else 1
        if args.action == "install":
            service.ensure_default_marketplace()
            summary = service.store.install(
                args.source, ref=args.ref, commit=args.commit, name=args.name
            )
            text = _install_text(summary)
            _print(summary, args.json, text)
            return 0
        if args.action == "trust":
            summary = service.store.trust(args.name)
            _print(
                summary,
                args.json,
                f"trusted {args.name}: {len(summary['plugins'])} plugins, {len(summary['agents'])} agents",
            )
            return 0
        if args.action == "update":
            summary = service.store.update(args.name)
            _print(summary, args.json, _install_text(summary))
            return 0
        if args.action == "uninstall":
            service.store.uninstall(args.name)
            _print({"uninstalled": args.name}, args.json, f"uninstalled {args.name}")
            return 0
        if args.action == "list":
            service.ensure_default_marketplace()
            markets = service.store.marketplaces()
            records = service.store.records()
            rows = []
            for name, entry in sorted(markets.items()):
                plugins = sorted(f"{r.id}@{r.version}" for r in records if r.marketplace == name)
                rows.append(
                    {
                        "name": name,
                        "trusted": entry.get("trusted"),
                        "source": entry.get("source"),
                        "commit": entry.get("commit"),
                        "plugins": plugins,
                    }
                )
            builtin = sorted(
                f"{r.id}@{r.version}" for r in records if r.marketplace == "clio-builtin"
            )
            rows.insert(
                0,
                {
                    "name": "clio-builtin",
                    "trusted": True,
                    "source": "clio-agent package",
                    "plugins": builtin,
                },
            )
            text = "\n".join(
                f"{r['name']} ({'trusted' if r['trusted'] else 'NOT TRUSTED'}) {r['source']}\n  "
                + ", ".join(r["plugins"])
                for r in rows
            )
            _print(rows, args.json, text)
            return 0
        if args.action == "agents":
            service.ensure_default_marketplace()
            rows = [entry.to_dict() for entry in service.agents()]
            _print(
                rows, args.json, "\n".join(f"{r['ref']}  {r['name']} {r['version']}" for r in rows)
            )
            return 0
        if args.action == "resolve":
            service.ensure_default_marketplace()
            resolution = service.resolve(
                args.agent, delta=Delta(tuple(args.add), tuple(args.remove)), pin=False
            )
            if args.prompt:
                text = resolution.prompt_text
            else:
                text = (
                    "\n".join(
                        f"{row['id']}@{row['version']} [{row['provenance']['label']}]"
                        for row in resolution.plugins
                    )
                    + "\nremoved: "
                    + ", ".join(
                        f"{r['id']} ({r['reason']} {r['because']})".strip()
                        for r in resolution.removed
                    )
                )
            _print(resolution.to_dict(), args.json, text)
            return 0
    except ResolutionError as exc:
        print("\n".join(f"error: {row}" for row in exc.diagnostics), file=sys.stderr)
        return 1
    except (StoreError, AgentNotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


def _install_text(summary: dict[str, Any]) -> str:
    lines = [
        f"installed {summary['marketplace']} ({summary['format']}): {len(summary['plugins'])} plugins, "
        f"{len(summary['agents'])} agents"
        + (
            ""
            if summary["trusted"]
            else f" -- run the trust step: clio-agent plugin trust {summary['marketplace']}"
        )
    ]
    if summary.get("pending_trust"):
        lines.append(
            "held until trusted (executable parts changed): " + ", ".join(summary["pending_trust"])
        )
    for row in summary.get("diagnostics") or []:
        lines.append(f"  {row['severity']}: {row['plugin']}: {row['message']} [{row['code']}]")
    for federated in summary.get("federated") or []:
        lines.append(_install_text(federated))
    return "\n".join(lines)
