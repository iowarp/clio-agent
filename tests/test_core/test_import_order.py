"""clio imports cleanly whatever the order relative to ``dspy`` (DSPy integration finding).

DSPy 3.4.0 (``dspy.utils.lazy_import``) puts unloaded proxies for ``anyio``, ``numpy``,
``openai`` and ``jiter`` in ``sys.modules``. Importing a submodule through an unloaded
proxy (``anyio.abc`` via FastAPI, ``numpy.linalg``, ``openai.types``) makes the parent
load half-way and fails (circular ``ImportError`` / ``TypeError``). Fixed on DSPy main
(stanfordnlp/dspy#10520), not yet released. Each check runs in a fresh interpreter:
the import order is the thing under test.
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
        "import dspy; import clio_agent; import numpy.linalg",
        "import clio_agent; import dspy; import numpy.linalg",
        "import clio_agent; import dspy; import openai.types",
        "import dspy; import clio_agent; import openai.types",
    ],
)
def test_clio_and_dspy_import_in_any_order(statement: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", statement], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
