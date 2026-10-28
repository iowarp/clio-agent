"""Dispatch standalone operator commands before the interactive CLI boots."""

from __future__ import annotations

from collections.abc import Sequence


def handle_local_command(arguments: Sequence[str]) -> bool:
    """Run a standalone verb, returning whether it handled the supplied arguments."""
    if not arguments:
        return False
    if arguments[0] == "migrate-paths":
        from clio_agent.path_migration import main as migrate_main

        migrate_main(list(arguments[1:]))
        return True
    if arguments[0] == "migrate":
        from clio_agent.gact.store_migrate_command import exit_released
        from clio_agent.gact.store_migrate_command import main as store_migrate_main

        exit_released(store_migrate_main(arguments[1:]))
    if arguments[0] == "plugin":
        from clio_agent.plugins.cli import run_plugin_cli

        raise SystemExit(run_plugin_cli(arguments[1:]))
    if arguments[0] == "mcp":
        from clio_agent.ui.mcp_cli import run_mcp_cli

        raise SystemExit(run_mcp_cli(arguments[1:]))
    return False
