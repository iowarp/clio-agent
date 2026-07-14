"""``python -m clio_agent.gact`` — the bundled-runtime entry point (#909).

The desktop bundled runtime execs the server as ``<python> -m
clio_agent.gact --host --port`` (generic manifest, iowarp/gact-tui#311),
so the module entry must exist and parse the same CLI as the console
script. Run as subprocesses: the point is the real interpreter-level
contract, not an import check.
"""

from __future__ import annotations

import subprocess
import sys


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-m", "clio_agent.gact", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_module_entry_help_exits_zero() -> None:
    proc = _run_module("--help")
    assert proc.returncode == 0, proc.stderr
    # The full serve CLI, not a stub: the launcher passes --host/--port.
    assert "--host" in proc.stdout
    assert "--port" in proc.stdout


def test_module_entry_rejects_unknown_flag() -> None:
    """argparse must own the CLI — an unknown flag is exit 2, not a crash."""
    proc = _run_module("--definitely-not-a-flag")
    assert proc.returncode == 2
    assert "unrecognized arguments" in proc.stderr
