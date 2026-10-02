"""The SDK-transport availability seam (finding #2, revised).

The SDK installs via the ``claude-code`` extra (its protective ``mcp<2`` bound
is neutralized by the uv dependency override — CLIO is an LLM-provider-only
consumer). When the SDK is genuinely absent, every ``sdk`` path must raise
uninstallable on the 2026-07-28 stack. Selecting the SDK transport when the
package is absent must yield a TYPED, structured unavailability error explaining
the mcp-2 incompatibility — never a bare ``ImportError`` traceback.
"""

from __future__ import annotations

import sys

import pytest

from clio_agent.providers import dependencies
from clio_agent.providers.claude_code_errors import ClaudeCodeCLIUnavailableError
from clio_agent.providers.claude_code_options import require_claude_agent_sdk


def _force_sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import claude_agent_sdk`` raise ImportError regardless of install state."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    def _fail_install() -> bool:
        raise dependencies.ProviderDependencyInstallError("offline")

    monkeypatch.setattr(dependencies, "ensure_claude_code_support", _fail_install)


def test_require_sdk_raises_typed_error_not_importerror(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_sdk_absent(monkeypatch)

    with pytest.raises(ClaudeCodeCLIUnavailableError) as excinfo:
        require_claude_agent_sdk()

    message = str(excinfo.value)
    assert message == (
        "CLIO could not install Claude Code support. "
        "Check the connected agent's internet connection and try again."
    )
    assert isinstance(excinfo.value, ClaudeCodeCLIUnavailableError)
    assert isinstance(excinfo.value.__cause__, dependencies.ProviderDependencyInstallError)


def test_an_engine_call_yields_the_typed_error_not_an_importerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's one SDK import seam surfaces the typed error before any send."""

    import asyncio

    from dspy.lm15 import Message, Request

    from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine

    _force_sdk_absent(monkeypatch)
    request = Request(model="claude_code/claude-x", messages=(Message.user("hi"),))

    with pytest.raises(ClaudeCodeCLIUnavailableError) as excinfo:
        asyncio.run(AsyncClaudeCodeEngine("claude-x").complete(request))
    assert "could not install Claude Code support" in str(excinfo.value)
