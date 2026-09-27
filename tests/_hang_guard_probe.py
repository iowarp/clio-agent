"""Inner tests for ``test_hang_guard.py`` (never collected by a normal run).

Two hangs the per-test limit must end, bracketed by passing tests that prove the run
continues after each: one parked in Python (the stock pytest-timeout thread timer
could catch it, but its dump never reaches an xdist controller) and one inside a
native call that holds the GIL (nothing in Python can run, so only the C watchdog in
``tests/_hang_guard.py`` can end it; this is the clio-core binding's failure shape).
"""

from __future__ import annotations

import ctypes
import sys
import threading

import pytest

# Function-only: the limit must land inside the hang, not in a cold worker's first
# fixture setup (a replacement worker re-imports the suite before its next test).
HANG_LIMIT_S = 3


def test_before_the_hangs() -> None:
    pass


@pytest.mark.timeout(HANG_LIMIT_S, func_only=True)
def test_hangs_in_python() -> None:
    threading.Event().wait()


@pytest.mark.timeout(HANG_LIMIT_S, func_only=True)
def test_hangs_holding_the_gil() -> None:
    # ctypes.PyDLL keeps the GIL across the foreign call (CDLL/WinDLL release it).
    if sys.platform == "win32":
        ctypes.PyDLL("kernel32").Sleep(600_000)
    else:
        ctypes.PyDLL(None).sleep(600)


def test_after_the_hangs() -> None:
    pass
