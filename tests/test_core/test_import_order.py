"""clio imports cleanly after ``import dspy`` (DSPy integration finding, 2026-10-01).

DSPy 3.4 puts a lazy proxy for ``anyio`` in ``sys.modules``. When FastAPI then imports
``anyio.abc`` before anything loaded anyio, ``anyio.abc`` sets an attribute on the proxy
mid-initialization; the proxy loads anyio, which re-imports the half-initialized
``anyio.abc`` -> ``ImportError`` (circular import). Each check runs in a fresh
interpreter: the import order is the thing under test.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "statement",
    [
        "import dspy; import clio_agent.gact.app",
        "import clio_agent.gact.app; import dspy",
    ],
)
def test_clio_and_dspy_import_in_either_order(statement: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", statement], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
