"""A failed or timed-out MCP client close is a WARNING with a typed reason (#1577 4).

Closing used to log only at debug, so a server that never finished closing (and may be
left running) was invisible. Drives the REAL ``AsyncMCPToolExecutor.aclose`` with client
contexts that hang or fail on exit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from clio_agent.tools import mcp_executor
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor


class _HangingExit:
    async def __aexit__(self, *_exc: Any) -> None:
        await asyncio.sleep(30)


class _FailingExit:
    async def __aexit__(self, *_exc: Any) -> None:
        raise RuntimeError("transport already gone")


async def test_close_timeouts_and_failures_warn_with_typed_reasons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SABOTAGE: log the close errors at debug again -> no WARNING records -> red."""
    executor = AsyncMCPToolExecutor(object(), timeout=0.2)
    executor._namespace_ctxs["fs"] = _FailingExit()
    executor._client_ctx = _HangingExit()
    with caplog.at_level(logging.WARNING, logger=mcp_executor.__name__):
        await executor.aclose()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("reason=mcp_close_failed" in m and "'fs'" in m for m in warnings), warnings
    assert any("reason=mcp_close_timeout" in m for m in warnings), warnings
