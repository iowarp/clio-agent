"""The suite-wide History mode guard (used by the autouse fixture in ``tests/conftest.py``).

The suite runs on clio-core. History mode is the platform-without-clio-core fallback, so a
test that enters it without the ``history_mode`` marker silently left clio-core: it fails.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from clio_agent.arc import history_mode


@contextmanager
def guard(node: Any) -> Iterator[None]:
    """Around one test: fail it if it entered History mode unmarked; forget the decision."""
    before = history_mode.entries()
    try:
        yield
    finally:
        entered = history_mode.entries() > before
        history_mode.reset_for_tests()
    if entered and node.get_closest_marker("history_mode") is None:
        pytest.fail(
            "this test entered History mode (no clio-core binding); mark it "
            "@pytest.mark.history_mode only if it tests that mode's basics",
            pytrace=False,
        )
