"""Environment construction for stdio MCP subprocesses."""

from __future__ import annotations

import os
from collections.abc import Mapping


def stdio_environment(spec_env: Mapping[str, str]) -> dict[str, str]:
    """Merge an MCP spec environment without leaking ambient Python overrides."""

    env = {**os.environ, **dict(spec_env)}
    for name in ("PYTHONHOME", "PYTHONPATH"):
        if name not in spec_env:
            env.pop(name, None)
    return env
