"""A failed download on the agent-load card says what actually happened, not only the
reason's stock words (a clio-kit exit 1 read as "does not know this server" while the
server was known)."""

from __future__ import annotations

from clio_agent.gact import agent_needs
from clio_agent.tools.mcp_prepare import PREPARE_REASONS, PROVISION_UNKNOWN_SERVER, PrepareError


def test_the_failing_commands_output_follows_the_reason() -> None:
    exc = PrepareError(
        PROVISION_UNKNOWN_SERVER,
        "clio-kit mcp-server --prepare graspa exited 1: error: lock held by another prepare",
    )
    words = agent_needs._words(PROVISION_UNKNOWN_SERVER, exc)
    assert words.startswith(PREPARE_REASONS[PROVISION_UNKNOWN_SERVER][:40])
    assert "What happened:" in words and "lock held by another prepare" in words


def test_an_unknown_reason_keeps_the_error_text() -> None:
    assert agent_needs._words("something_else", RuntimeError("boom\n twice")) == "boom twice"
