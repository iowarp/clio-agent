"""Environment isolation helper for provider-config tests (#902).

Provider-detection tests need a clean slate free of ambient ``CLIO_*`` /
``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / ... variables, and reached for
``patch.dict("os.environ", ..., clear=True)`` to get one. ``clear=True`` removes
*every* variable from the process environment for the duration of the ``with``
block -- including ``PATH`` (and ``PATHEXT`` on Windows).

That opens a window in which ``shutil.which("sh")`` (and any other launcher
lookup) returns ``None``, because the executable search has no directories to
scan. It is a process-global mutation: a background MCP launcher thread or
subprocess that runs concurrently (the orphaned-child class #900 tightened)
observes the emptied ``PATH`` and dies with ``'sh ... not found on PATH'``. Under
``pytest-randomly`` this surfaced as ~7 spurious failures in an unrelated file,
``tests/test_tools/test_mcp_config.py`` (whose ``transport_for`` tests call
``shutil.which("sh")``), whenever such a window overlapped their run (#902).

:func:`isolated_environ` clears the application variables the tests care about
while preserving the OS-essential ones (``PATH`` and friends), so the process
environment never loses its executable search path and no concurrent launcher
lookup can spuriously fail.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from unittest.mock import patch

# OS-essential variables that must survive environment isolation. Without PATH a
# concurrent ``shutil.which()`` finds nothing; PATHEXT/SystemRoot/COMSPEC/... keep
# process spawning functional on Windows. These are never what a provider-config
# test intends to remove -- only the ``CLIO_*``/API-key application vars are.
_PRESERVED_KEYS: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
)


def isolated_environ(overrides: dict[str, str] | None = None) -> AbstractContextManager[None]:
    """Isolate application env vars while preserving the executable search path.

    A drop-in replacement for ``patch.dict("os.environ", overrides, clear=True)``
    that keeps the OS-essential variables in :data:`_PRESERVED_KEYS` present. The
    application variables (``CLIO_*``, provider API keys, ...) are still cleared,
    so provider-detection semantics are unchanged, but ``PATH`` never disappears
    from the process environment (see the module docstring for why that matters).

    Args:
        overrides: Variables to set inside the isolated environment. Everything
            else is cleared except the preserved OS-essential keys.

    Returns:
        The ``patch.dict`` context manager, ready to be used in a ``with`` block.
    """
    base = {key: os.environ[key] for key in _PRESERVED_KEYS if key in os.environ}
    if overrides:
        base.update(overrides)
    return patch.dict("os.environ", base, clear=True)
