# pyright: reportPrivateUsage=false
"""Causal coverage for schema-declared MCP timeouts and uncertain mutations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Self

import pytest

from clio_agent.tools.execution import (
    AsyncMCPToolExecutor,
    SyncMCPToolExecutor,
    UncertainMutatingToolOutcomeError,
)


class _Client:
    """Small protocol-complete MCP client with controllable latency and failures."""

    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        errors: list[BaseException | None] | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.errors = list(errors or [])
        self.calls = 0
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        self.closed = True
        return None

    async def list_tools(self) -> list[Any]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return SimpleNamespace(data={"name": name, "args": arguments})

    async def read_resource(self, uri: str) -> Any:
        return SimpleNamespace(uri=uri)


def _relay_jarvis_run_tool(*, idempotent: bool = False) -> SimpleNamespace:
    """Return the live relay run contract shape relevant to timeout semantics."""

    return SimpleNamespace(
        name="relay_jarvis_run",
        description="Submit and optionally wait for a remote JARVIS execution.",
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "wait_for_terminal": {"type": "boolean", "default": False},
                "wait_timeout_seconds": {"type": "number", "default": 600},
            },
        },
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": idempotent,
        },
    )


@pytest.mark.asyncio
async def test_explicit_terminal_wait_timeout_extends_executor_deadline() -> None:
    """A schema-declared wait budget must outrank the generic 30s-style default."""

    client = _Client(delay_seconds=0.05)
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )
    await executor.start()

    try:
        result = await executor.call_tool(
            "relay_jarvis_run",
            {
                "pipeline_id": "asteroid",
                "timeout_seconds": 0.02,
                "wait_for_terminal": True,
                "wait_timeout_seconds": 0.1,
            },
        )
    finally:
        await executor.aclose()

    assert '"pipeline_id": "asteroid"' in result
    assert client.calls == 1


@pytest.mark.asyncio
async def test_live_relay_timeout_budgets_include_every_declared_phase() -> None:
    """The live 120s operation plus 600s wait retain 30s for transport overhead."""

    client = _Client()
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=30,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )
    await executor.start()

    try:
        budget = executor._timeout_budget_for_call(
            "relay_jarvis_run",
            {
                "pipeline_id": "asteroid",
                "timeout_seconds": 120,
                "wait_for_terminal": True,
                "wait_timeout_seconds": 600,
            },
        )
    finally:
        await executor.aclose()

    assert budget.seconds == 750
    assert budget.explicitly_declared is True


def test_sync_wrapper_does_not_undercut_async_declared_deadline() -> None:
    """The sync adapter must use the same extended budget as the async call."""

    client = _Client(delay_seconds=0.05)
    executor = SyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )

    try:
        result = executor.call_tool(
            "relay_jarvis_run",
            {
                "pipeline_id": "asteroid",
                "timeout_seconds": 0.02,
                "wait_for_terminal": True,
                "wait_timeout_seconds": 0.1,
            },
        )
    finally:
        executor.close()

    assert '"pipeline_id": "asteroid"' in result
    assert client.calls == 1


@pytest.mark.asyncio
async def test_mutating_tool_without_explicit_timeout_keeps_default_and_fences_retry() -> None:
    """A default deadline stays 30s-style but remains an uncertain mutation."""

    client = _Client(delay_seconds=0.05)
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )
    await executor.start()

    try:
        with pytest.raises(
            UncertainMutatingToolOutcomeError,
            match="status='outcome_unknown'.*timeout_seconds=0.01",
        ):
            await executor.call_tool(
                "relay_jarvis_run",
                {"pipeline_id": "asteroid"},
            )
        with pytest.raises(
            UncertainMutatingToolOutcomeError,
            match="prior uncertain timeout blocks this retry",
        ):
            await executor.call_tool(
                "relay_jarvis_run",
                {"pipeline_id": "asteroid"},
            )
    finally:
        await executor.aclose()

    assert client.calls == 1


@pytest.mark.asyncio
async def test_read_only_tool_without_explicit_timeout_keeps_existing_timeout() -> None:
    """A read-only call retains the ordinary retryable default-timeout behavior."""

    client = _Client(delay_seconds=0.05)
    tool = _relay_jarvis_run_tool()
    tool.annotations["readOnlyHint"] = True
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": tool},
    )
    await executor.start()

    try:
        with pytest.raises(TimeoutError, match="timed out after 0.01s"):
            await executor.call_tool(
                "relay_jarvis_run",
                {"pipeline_id": "asteroid"},
            )
    finally:
        await executor.aclose()

    assert client.calls == 1


def test_uncertain_mutating_timeout_blocks_blind_retry() -> None:
    """No-result timeout of a non-idempotent mutation must fence a duplicate call."""

    client = _Client(errors=[TimeoutError("remote result was not received")])
    executor = SyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool()},
    )
    args = {
        "pipeline_id": "asteroid",
        "timeout_seconds": 120,
        "wait_for_terminal": True,
        "wait_timeout_seconds": 600,
    }

    try:
        with pytest.raises(
            UncertainMutatingToolOutcomeError,
            match="status='outcome_unknown'.*retry_safe=False.*action='do_not_retry'",
        ):
            executor.call_tool("relay_jarvis_run", args)
        with pytest.raises(
            UncertainMutatingToolOutcomeError,
            match="prior uncertain timeout blocks this retry",
        ):
            executor.call_tool("relay_jarvis_run", args)
    finally:
        executor.close()

    assert client.calls == 1


def test_explicit_idempotent_timeout_keeps_existing_retry_behavior() -> None:
    """Protocol-declared idempotency keeps ordinary timeout/retry semantics."""

    client = _Client(errors=[TimeoutError("first timeout"), None])
    executor = SyncMCPToolExecutor(
        object(),
        timeout=0.01,
        client_factory=lambda _server: client,
        preloaded_tools={"relay_jarvis_run": _relay_jarvis_run_tool(idempotent=True)},
    )
    args = {"pipeline_id": "asteroid", "timeout_seconds": 0.02}

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            executor.call_tool("relay_jarvis_run", args)
        result = executor.call_tool("relay_jarvis_run", args)
    finally:
        executor.close()

    assert '"pipeline_id": "asteroid"' in result
    assert client.calls == 2
