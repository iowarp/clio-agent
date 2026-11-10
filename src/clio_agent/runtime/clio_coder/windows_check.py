"""Run a Windows acceptance command without C-argv quoting its shell syntax."""

from __future__ import annotations

import os
import subprocess
import sys


def run_check(command: str) -> int:
    """Pass the original string to Python's native cmd shell adapter."""
    if sys.platform != "win32":
        raise RuntimeError("Windows acceptance checks require a Windows host")
    return subprocess.run(
        command,
        shell=True,
        executable=os.environ.get("COMSPEC", "cmd.exe"),
        stdin=subprocess.DEVNULL,
        check=False,
    ).returncode


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("Expected one Windows acceptance command")
    sys.exit(run_check(sys.argv[1]))
