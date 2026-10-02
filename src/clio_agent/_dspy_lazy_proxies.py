"""Load the modules DSPy 3.4.0 leaves as lazy proxies (a DSPy integration finding).

DSPy 3.4.0 (``dspy.utils.lazy_import``) puts unloaded proxies for some third-party
modules (``anyio``, ``numpy``, ``openai``, ``jiter``) in ``sys.modules``. Importing a
submodule through an unloaded proxy -- ``anyio.abc`` via FastAPI, ``numpy.linalg``,
``openai.types`` -- loads the parent half-way and fails (a circular ``ImportError`` or
``TypeError``). Each proxy is loaded as soon as ``dspy`` finishes importing. Fixed on
DSPy main (stanfordnlp/dspy#10520): remove this module once clio's ``dspy`` floor has it.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any

_PROXY_MODULE = "dspy.utils.lazy_import"
_PROXY_CLASS = "_LazyModule"


def load_proxies() -> None:
    """Load every module DSPy left in ``sys.modules`` as an unloaded lazy proxy."""
    for module in list(sys.modules.values()):
        kind = type(module)
        if kind.__module__ == _PROXY_MODULE and kind.__name__ == _PROXY_CLASS:
            module._load()  # the proxy's own loader (what its ``__getattr__`` calls)


class _LoadProxiesAfterDspy(importlib.abc.MetaPathFinder):
    """Runs :func:`load_proxies` right after ``dspy`` executes."""

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "dspy":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        loader: Any = spec.loader
        exec_module = loader.exec_module

        def exec_then_load(module: ModuleType) -> None:
            exec_module(module)
            load_proxies()

        loader.exec_module = exec_then_load
        return spec


def install() -> None:
    """Load DSPy's proxies now if ``dspy`` is imported, else right after it is."""
    if "dspy" in sys.modules:
        load_proxies()
    elif not any(isinstance(f, _LoadProxiesAfterDspy) for f in sys.meta_path):
        sys.meta_path.insert(0, _LoadProxiesAfterDspy())
