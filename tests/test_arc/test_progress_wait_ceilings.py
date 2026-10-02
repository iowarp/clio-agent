"""The progress-based waits give up at 3 minutes by default (owner, 2026-10-02).

A daemon or MCP server that is visibly working is waited for; the ceiling bounds even a
busy one that never answers. 600 s made a stuck daemon cost ten minutes before its typed
failure; the owner set the default to 180 s.
"""

from __future__ import annotations

import pytest

from clio_agent.arc import daemon_progress
from clio_agent.tools import mcp_probe_hardening


def test_the_clio_core_wait_ceiling_defaults_to_three_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIO_ARC_LIVENESS_MAX_WAIT_S", raising=False)
    assert daemon_progress.max_wait_s() == 180.0


def test_the_mcp_start_wait_ceiling_defaults_to_three_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIO_MCP_MAX_WAIT_S", raising=False)
    assert mcp_probe_hardening.mcp_max_wait_s() == 180.0
