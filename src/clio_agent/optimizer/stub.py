"""Canonical "optimizer not implemented" stub for every user-facing entry point.

Research-pending (#801 owner decision, tracked in
https://github.com/iowarp/clio-agent/issues/633): the optimizer vertical is
kept as planned research, and until it lands every user-facing path into it
(the gact ``/optimize`` command row, the ``optimizer_command`` capability-gap
row, and the ``--tune`` CLI hook) must return the same structured
not-implemented response defined here — no path may silently half-run.

This module is deliberately dependency-free (stdlib only) so the gact runtime
leaves (:mod:`clio_agent.gact.runtime.commands`,
:mod:`clio_agent.gact.runtime.capabilities`) can import it without pulling
DSPy or the rest of the optimizer package.
"""

from __future__ import annotations

from typing import Any

OPTIMIZER_NOT_IMPLEMENTED_REASON = "optimizer_not_implemented"
"""Machine-readable reason code shared by every optimizer entry point."""

OPTIMIZER_TRACKING_ISSUE = "https://github.com/iowarp/clio-agent/issues/633"
"""Where the optimizer research work (implementing /optimize) is tracked."""

OPTIMIZER_NOT_IMPLEMENTED_MESSAGE = (
    "The optimizer is a planned CLIO research surface and is not implemented "
    f"yet; optimization runs are tracked in {OPTIMIZER_TRACKING_ISSUE}."
)
"""User-facing description shared by every optimizer entry point."""


def optimizer_not_implemented_payload() -> dict[str, Any]:
    """Return the uniform structured not-implemented response.

    Every user-facing optimizer entry point either returns this payload
    directly (CLI ``--tune``) or projects its fields into its own wire shape
    (gact command row ``error``/``disabled_reason``, capability-gap row
    ``reason``/``tracking_issue``/``description``).

    Returns:
        Dict with ``implemented`` (always ``False``), ``reason`` (the shared
        reason code), ``message`` (human-readable), and ``tracking_issue``
        (pointer to #633).
    """
    return {
        "implemented": False,
        "reason": OPTIMIZER_NOT_IMPLEMENTED_REASON,
        "message": OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
        "tracking_issue": OPTIMIZER_TRACKING_ISSUE,
    }


def run_tune_cli(expert_id: str, *, json_output: bool = False) -> int:
    """Render the ``--tune`` not-implemented stub and return its exit code (2).

    The CLI ``--tune`` hook dispatches here (no accretion in ``ui/cli.py``): it prints the
    uniform structured not-implemented payload (JSON or a one-line notice) and returns exit
    code ``2`` so the research surface never silently half-runs.
    """
    payload = {"expert_id": expert_id, **optimizer_not_implemented_payload()}
    if json_output:
        import json  # noqa: PLC0415

        print(json.dumps(payload))
    else:
        from rich.console import Console  # noqa: PLC0415

        Console().print(f"[yellow]{payload['message']}[/yellow]")
    return 2
