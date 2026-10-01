"""The MCP handshake probe waits for a server that is visibly starting (no fixed deadline).

Found live (leg B, 2026-10-01): the clio-kit web server's handshake row read "did not respond
within 20s" -- a fixed wall-clock deadline, so a slow machine or a first launch (uv
installing, Python importing) reads as a dead server. A slice that passes while the server's
process tree works keeps waiting, up to ``tools.mcp.max_wait_s``; only a whole slice with no
progress is a timeout.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from clio_agent.arc import daemon_progress
from clio_agent.tools import mcp_probe_hardening
from clio_agent.tools.mcp_probe_hardening import NoProgressTimeout, wait_while_server_works


async def _finishes_after(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "tools"


def test_a_server_still_working_is_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count()
    monkeypatch.setattr(daemon_progress, "descendants_work", lambda: float(next(counter)))

    result = asyncio.run(wait_while_server_works(_finishes_after(0.35), slice_s=0.1))

    assert result == "tools"


def test_a_whole_slice_without_progress_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_progress, "descendants_work", lambda: 5.0)

    with pytest.raises(NoProgressTimeout):
        asyncio.run(wait_while_server_works(_finishes_after(5.0), slice_s=0.1))


def test_the_ceiling_bounds_even_a_working_server(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count()
    monkeypatch.setattr(daemon_progress, "descendants_work", lambda: float(next(counter)))
    monkeypatch.setattr(mcp_probe_hardening, "mcp_max_wait_s", lambda: 0.3)

    with pytest.raises(NoProgressTimeout):
        asyncio.run(wait_while_server_works(_finishes_after(5.0), slice_s=0.1))
