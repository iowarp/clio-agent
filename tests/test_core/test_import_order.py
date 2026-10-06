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


def test_proxy_loading_waits_for_an_in_progress_dspy_import() -> None:
    """Hold the real DSPy loader and prove proxy loading honors its import lock."""
    script = """
import importlib.abc
import importlib.machinery
import sys
import threading
from clio_agent import _dspy_lazy_proxies

started = threading.Event()
release = threading.Event()
entered = threading.Event()
finished = threading.Event()
errors = []

class Gate(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'dspy':
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        original = spec.loader.exec_module
        def execute(module):
            started.set()
            assert release.wait(10), 'test did not release the DSPy import'
            original(module)
        spec.loader.exec_module = execute
        return spec

def import_dspy():
    try:
        import dspy
    except BaseException as exc:
        errors.append(exc)

def load_proxies():
    entered.set()
    try:
        _dspy_lazy_proxies.install()
    except BaseException as exc:
        errors.append(exc)
    finally:
        finished.set()

sys.meta_path.insert(0, Gate())
importer = threading.Thread(target=import_dspy)
loader = threading.Thread(target=load_proxies)
importer.start()
try:
    assert started.wait(10), 'DSPy import did not start'
    loader.start()
    assert entered.wait(10), 'proxy loader did not start'
    assert not finished.wait(0.1), 'proxy loading raced the unfinished DSPy import'
finally:
    release.set()
    importer.join(30)
    if loader.ident is not None:
        loader.join(30)
assert not importer.is_alive() and not loader.is_alive()
assert not errors, errors
assert finished.is_set()
import numpy.linalg
import openai.types
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-3000:]
