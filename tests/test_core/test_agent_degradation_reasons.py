"""Structured-degradation-reason tests for ClioAgent fallback paths (#772).

Each ARC-degradation path in ``ClioAgent`` used to warn only via
``if self.verbose: print(...)`` — silent in server deployments. These tests pin
that every fallback now emits a structured, reason-coded log entry so the
degradation reaches the trace/API instead of vanishing.
"""

from __future__ import annotations

import logging

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.harness import RouteDecision

AGENT_LOGGER = "clio_agent.agent"


@pytest.fixture
def agent() -> ClioAgent:
    """A real ClioAgent whose ARC surfaces are stubbed per-test."""
    a = ClioAgent()
    try:
        yield a
    finally:
        a.shutdown()


class _Boom:
    """Any attribute access returns a callable that raises."""

    def __init__(self, message: str = "boom") -> None:
        self._message = message

    def __getattr__(self, _name: str):
        def _raise(*_args, **_kwargs):
            raise RuntimeError(self._message)

        return _raise


def test_context_compile_failure_is_reason_coded(
    agent: ClioAgent, caplog: pytest.LogCaptureFixture
) -> None:
    """Compiler + legacy retrieval both failing → both reasons logged."""
    agent.context_retriever = _Boom("compiler down")  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger=AGENT_LOGGER):
        result = agent._get_session_context("q", "sess-1", tool_scope="chat")

    assert result == "No prior context"
    assert "reason=context_compile_failed" in caplog.text
    assert "reason=context_unavailable" in caplog.text


def test_routing_decision_store_failure_is_reason_coded(
    agent: ClioAgent, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising ARC store leaves the turn intact but logs the reason."""
    agent.arc = _Boom("arc down")  # type: ignore[assignment]
    route = RouteDecision(
        target="chat", source="native", reason="test", confidence=1.0
    )

    with caplog.at_level(logging.WARNING, logger=AGENT_LOGGER):
        agent._store_routing_decision("q", route, "sess-1")

    assert "reason=routing_decision_store_failed" in caplog.text


def test_invocation_store_failure_is_reason_coded(
    agent: ClioAgent, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising invocation store logs a reason rather than swallowing."""
    agent.arc = _Boom("arc down")  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger=AGENT_LOGGER):
        agent._store_expert_invocation(
            question="q",
            file_context="",
            selected="data",
            session_id="sess-1",
            expert_result=None,
            success=False,
            error_msg="oops",
            duration_ms=1.0,
        )

    assert "reason=invocation_store_failed" in caplog.text


def test_last_session_file_lookup_failure_is_reason_coded(
    agent: ClioAgent, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising conversation lookup returns None and debug-logs the reason."""
    agent.arc = _Boom("arc down")  # type: ignore[assignment]

    with caplog.at_level(logging.DEBUG, logger=AGENT_LOGGER):
        result = agent._last_session_file_path("sess-1")

    assert result is None
    assert "reason=session_file_lookup_failed" in caplog.text
