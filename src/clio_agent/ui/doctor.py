"""Doctor CLI rendering + the disk-GC runner (owner module, split out of ``ui/cli.py``).

``ui/cli.py`` is a size-ratcheted god-file (iowarp/clio-agent#714/#774); the doctor
rendering (``render_doctor_report``) and the #1001 disk-GC surface (``render_gc_report`` /
``run_doctor_gc``) live here instead of accreting onto it. ``cli.py`` imports these so the
public entry points (``clio-agent doctor`` / ``doctor --gc``) are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

__all__ = ["render_doctor_report", "render_gc_report", "run_doctor_gc"]


def render_doctor_report(console: Console, report: Any) -> None:
    """Render an in-process runtime doctor report (``collect_runtime_status``).

    Reads :class:`~clio_agent.runtime.status.IntegrationStatus.state`; used by
    the standalone ``doctor`` subcommand, which must run without a server.
    """
    status_styles = {
        "ready": "green",
        "degraded": "yellow",
        "unavailable": "red",
        "misconfigured": "red",
        "skipped": "cyan",
    }
    title = f"CLIO Runtime Doctor ({report.overall_status})"
    table = Table(title=title, show_header=True)
    table.add_column("Integration", style="cyan")
    table.add_column("Status")
    table.add_column("Summary")
    table.add_column("Config Source")
    table.add_column("Endpoint")
    table.add_column("Next Action")

    for item in report.integrations:
        state = item.state.value
        style = status_styles.get(state, "white")
        table.add_row(
            item.name,
            f"[{style}]{state}[/{style}]",
            escape(item.summary),
            escape(item.config_source),
            escape(item.endpoint or ""),
            escape(item.next_action),
        )

    console.print(table)


def render_gc_report(console: Console, report: Any) -> None:
    """Render a :class:`~clio_agent.runtime.disk_gc.GCReport` as a summary table."""
    from clio_agent.runtime.humanize import format_bytes  # noqa: PLC0415

    if report.refused:
        console.print(
            f"[red]clio doctor --gc REFUSED:[/red] {escape(report.refusal_reason)} — "
            "a clio server/daemon/MCP process is alive. Stop clio first (pruning a uv "
            "cache under a live spawner corrupts it)."
        )
        for peer in report.peers:
            console.print(f"  [yellow]live peer:[/yellow] {escape(str(peer))}")
        return

    mode = "DRY-RUN (nothing deleted)" if report.dry_run else "reclaimed"
    table = Table(title=f"CLIO Disk GC ({mode})", show_header=True)
    table.add_column("Location", style="cyan")
    table.add_column("Freed")
    table.add_column("Removed")
    table.add_column("Kept")
    table.add_column("Reason")
    table.add_column("Detail")
    for loc in report.locations:
        freed = "n/a" if loc.bytes_freed is None else format_bytes(loc.bytes_freed)
        table.add_row(
            loc.location,
            freed,
            str(loc.removed),
            str(loc.kept),
            escape(loc.reason),
            escape(loc.detail),
        )
    console.print(table)
    console.print(
        f"[bold]Total {'reclaimable' if report.dry_run else 'freed'}:[/bold] "
        f"{format_bytes(report.total_bytes_freed)}"
    )


def run_doctor_gc(*, dry_run: bool = False, json_output: bool = False) -> int:
    """Run ``clio doctor --gc`` IN-PROCESS: the manual/recovery disk reclamation (#1001).

    Refuses (typed, non-zero exit) when a live clio peer is present; otherwise prunes the
    clio-owned cache locations and prints a summary. ``dry_run`` computes the plan only.
    """
    from clio_agent.runtime.disk_gc import run_gc  # noqa: PLC0415

    report = run_gc(dry_run=dry_run)
    if json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        render_gc_report(Console(), report)
    return 1 if report.refused else 0
