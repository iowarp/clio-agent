"""Importing CLIO keeps ``platform.machine()`` truthful (it skips only the WMI probe)."""

from __future__ import annotations

import os
import subprocess
import sys


def test_importing_clio_keeps_the_machine_architecture() -> None:
    """SABOTAGE: seed an empty ``machine`` -> the Copilot SDK names no runtime -> red."""
    env = {**os.environ, "PROCESSOR_ARCHITECTURE": "AMD64"}
    env.pop("PROCESSOR_ARCHITEW6432", None)
    code = "import clio_agent, platform; print(platform.machine())"
    machine = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert machine == ("AMD64" if os.name == "nt" else os.uname().machine)
