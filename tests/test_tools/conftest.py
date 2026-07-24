"""Test-tools isolation: pin the OS write-confinement floor for every tool test (#976 B2).

The tool-surface tests (``mcp_config`` transport composition, ``shell_server`` bash,
``spawn_diet`` plans) all route spawns through
:func:`clio_agent.runtime.sandbox.wrap_confined` and assert PASSTHROUGH argv (they predate the
B2 fence activation). On a Landlock-capable Linux host the ladder activates for real and the
shim is prepended, breaking those assertions. None of these tests exercise fence ACTIVATION, so
the floor is pinned for all of them (autouse) — the environment-conformance rule: vary CONFIG,
never the ambient box capability.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _floor_sandbox_for_tools(floor_sandbox):
    """Apply the shared ``floor_sandbox`` fixture to every test in ``tests/test_tools``."""
    return floor_sandbox
