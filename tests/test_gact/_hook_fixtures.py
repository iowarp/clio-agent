"""Shared helpers for exercising the P2.2 hook dispatcher in tests.

Builds real subprocess hooks (the industry exit-0/exit-2 wire) from small Python
scripts run with the current interpreter, so the tests drive the SAME adapter path
production uses — no mocking of the transport.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any

from clio_agent.gact.hooks import HookDispatcher, parse_hook_entries


def write_hook_script(tmp_path: Path, name: str, body: str) -> Path:
    """Write a Python hook script and return its path (marked executable)."""

    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def command_run(script: Path, *extra_args: str) -> dict[str, Any]:
    """Return a ``run`` block invoking ``script`` with the current interpreter."""

    return {"type": "command", "command": sys.executable, "args": [str(script), *extra_args]}


def dispatcher_from_rows(rows: list[dict[str, Any]]) -> HookDispatcher:
    """Build a dispatcher from raw config rows (exercises config parsing)."""

    return HookDispatcher(parse_hook_entries(rows, source="test"))


def make_command_dispatcher(
    tmp_path: Path,
    *,
    event: str,
    body: str,
    hook_id: str = "test-hook",
    match: dict[str, Any] | None = None,
    fail_closed: bool = False,
    timeout_ms: int = 30000,
    script_name: str = "hook.py",
) -> HookDispatcher:
    """Build a dispatcher with one subprocess hook for ``event`` running ``body``."""

    script = write_hook_script(tmp_path, script_name, body)
    row: dict[str, Any] = {
        "id": hook_id,
        "on": [event],
        "run": command_run(script),
        "failClosed": fail_closed,
        "timeoutMs": timeout_ms,
    }
    if match is not None:
        row["match"] = match
    return dispatcher_from_rows([row])
