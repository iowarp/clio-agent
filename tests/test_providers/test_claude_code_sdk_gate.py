"""The SDK-transport availability gate on the fastmcp-4 / mcp-2 stack (finding #2).

``claude-agent-sdk`` is upstream-declared incompatible with mcp 2 (it pins
``mcp<2`` from 0.2.96), so the ``claude-code`` extra is intentionally
uninstallable on the 2026-07-28 stack. Selecting the SDK transport when the
package is absent must yield a TYPED, structured unavailability error explaining
the mcp-2 incompatibility — never a bare ``ImportError`` traceback.
"""

from __future__ import annotations

import sys

import pytest

from clio_agent.providers.claude_code_litellm import ClaudeCodeCLIUnavailableError
from clio_agent.providers.claude_code_options import require_claude_agent_sdk


def _force_sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import claude_agent_sdk`` raise ImportError regardless of install state."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)


def test_require_sdk_raises_typed_error_not_importerror(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_sdk_absent(monkeypatch)

    with pytest.raises(ClaudeCodeCLIUnavailableError) as excinfo:
        require_claude_agent_sdk()

    message = str(excinfo.value)
    # The reason names the mcp-2 incompatibility (not a raw ImportError trace).
    assert "mcp" in message.lower()
    assert "claude-agent-sdk" in message
    # It is a typed error, and its cause is the underlying ImportError.
    assert isinstance(excinfo.value, ClaudeCodeCLIUnavailableError)
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_selecting_sdk_transport_stream_yields_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming SDK seam surfaces the typed error, not an ImportError trace."""

    import asyncio

    from clio_agent.providers import claude_code_litellm

    _force_sdk_absent(monkeypatch)

    async def _drive() -> None:
        stream = claude_code_litellm._astream_sdk(prompt="hi", model="claude-x")
        await stream.__anext__()

    with pytest.raises(ClaudeCodeCLIUnavailableError) as excinfo:
        asyncio.run(_drive())
    assert "mcp" in str(excinfo.value).lower()


def test_sdk_pool_complete_yields_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pooled SDK seam surfaces the typed error, not an ImportError trace."""

    from clio_agent.providers.claude_code_sdk_pool import _SdkSession

    _force_sdk_absent(monkeypatch)

    session = _SdkSession()
    with pytest.raises(ClaudeCodeCLIUnavailableError) as excinfo:
        session.complete(prompt="hi", model="claude-x", timeout=5.0, cwd=None)
    assert "mcp" in str(excinfo.value).lower()
