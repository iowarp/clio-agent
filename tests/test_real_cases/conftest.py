"""Real-cases tier: live, end-to-end CLIO blueprint case tests.

This tier sits a level above unit and integration. A real case runs an actual
CLIO session through a marketplace Agent Blueprint against a live provider, then
asserts on the normalized trace. These tests are slow and hit external services,
so they are skipped unless ``CLIO_RUN_LIVE=1`` is set.

Provider/model are not hardcoded: the SUT discovers cells from the live provider
registry (or ``CLIO_AGENTTEST_CELLS``); pin one with ``pytest --provider/--model``
or fan out with ``pytest --matrix``.
"""

from __future__ import annotations

import os

import pytest

from . import clio_sut  # noqa: F401  — subclassing SUT registers it for agent-test


def pytest_collection_modifyitems(config, items) -> None:
    """Skip live real-case tests unless explicitly enabled."""
    if os.environ.get("CLIO_RUN_LIVE"):
        return
    skip_live = pytest.mark.skip(reason="real-case live test; set CLIO_RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
