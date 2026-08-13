"""Pins for the hardened auto-negotiation policy (owner ruling 2026-08-13).

Protocol/era selection comes ONLY from the server's typed answers — a
client-side timeout retries the SAME probe and NEVER falls back to the legacy
``initialize`` (the #1186 race class: slow spawn ⇒ silent era downgrade ⇒
-32022 death against a v2-only server).
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT, UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import LATEST_MODERN_VERSION

from clio_agent.tools.mcp_probe_hardening import (
    hardened_negotiate_auto,
    install_probe_hardening,
)


class _FakeSession:
    """Scripted session: pops one behavior per send_discover/initialize call."""

    def __init__(self, discover_script: list[Any], initialize_script: list[Any] | None = None):
        self.discover_script = list(discover_script)
        self.initialize_script = list(initialize_script or [])
        self.discover_calls: list[str] = []
        self.initialize_calls = 0
        self.adopted: Any = None

    async def send_discover(self, version: str) -> Any:
        self.discover_calls.append(version)
        step = self.discover_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def initialize(self) -> Any:
        self.initialize_calls += 1
        if self.initialize_script:
            step = self.initialize_script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return {"legacy": True}

    def adopt(self, result: Any) -> None:
        self.adopted = result


def _timeout() -> MCPError:
    return MCPError(code=REQUEST_TIMEOUT, message="Request 'server/discover' timed out")


def _good_discover() -> dict[str, Any]:
    return {"supportedVersions": [LATEST_MODERN_VERSION], "capabilities": {}}


@pytest.mark.asyncio
async def test_timeout_retries_the_same_probe_and_never_initializes() -> None:
    """Two timeouts then a good answer → modern adopted, ZERO initialize calls.

    **Sabotage:** restore the SDK's denylist branch for REQUEST_TIMEOUT → the
    first timeout falls back to initialize → initialize_calls > 0 → red.
    """

    session = _FakeSession([_timeout(), _timeout(), _good_discover()])
    await hardened_negotiate_auto(session)
    assert session.adopted is not None
    assert session.initialize_calls == 0
    assert session.discover_calls == [LATEST_MODERN_VERSION] * 3


@pytest.mark.asyncio
async def test_timeout_exhaustion_raises_typed_never_downgrades() -> None:
    """All probes time out → the timeout raises; the era is NEVER switched."""

    session = _FakeSession([_timeout()] * 10)
    with pytest.raises(MCPError) as exc_info:
        await hardened_negotiate_auto(session)
    assert exc_info.value.code == REQUEST_TIMEOUT
    assert session.initialize_calls == 0


@pytest.mark.asyncio
async def test_typed_method_not_found_still_falls_back_to_legacy() -> None:
    """A genuine typed rpc error keeps the SDK's denylist fallback (unchanged)."""

    session = _FakeSession([MCPError(code=-32601, message="method not found")])
    await hardened_negotiate_auto(session)
    assert session.initialize_calls == 1
    assert session.adopted is None


@pytest.mark.asyncio
async def test_disjoint_modern_only_refusal_raises() -> None:
    """-32022 with a disjoint modern-only supported list is a real incompatibility."""

    err = MCPError(
        code=UNSUPPORTED_PROTOCOL_VERSION,
        message="unsupported",
        data={"supported": ["9999-01-01"], "requested": LATEST_MODERN_VERSION},
    )
    session = _FakeSession([err, err])
    with pytest.raises(MCPError):
        await hardened_negotiate_auto(session)
    assert session.initialize_calls == 0


def test_install_swaps_both_import_bindings() -> None:
    """Both the SDK module and fastmcp's from-import binding point at the hardened fn."""

    import fastmcp.client.client as fastmcp_client
    import mcp.client._probe as sdk_probe

    install_probe_hardening()
    assert sdk_probe.negotiate_auto is hardened_negotiate_auto
    assert fastmcp_client.negotiate_auto is hardened_negotiate_auto
