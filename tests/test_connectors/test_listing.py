"""A connector keeps its last tools while it waits on the person (CN5, AQ6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp.types import Tool

from clio_agent.connectors import listing, state
from clio_agent.gact import mcp_readiness
from clio_agent.tools.mcp_sign_in import MCPSignInNeeded


def _proxied_error() -> Exception:
    """What a listing through an in-process proxy raises: a protocol error, untyped."""

    return RuntimeError("Client failed to connect: mcp_sign_in_needed: fixture-signin")


def _executor() -> Any:
    merged: dict[str, Any] = {}
    return SimpleNamespace(
        merge_namespace_tools=lambda _namespace, tools: merged.update(tools), merged=merged
    )


def test_held_for_setup_reads_the_typed_error_or_the_connectors_own_state(
    fixture_catalog: Path,
) -> None:
    # The typed error is enough on its own.
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = MCPSignInNeeded("fixture-signin")
    assert listing.held_for_setup("fixture-plain", wrapped)
    # Without it, CLIO's record decides: the sign-in connector has no account here.
    state.update("fixture-signin", enabled=True, values={"address": "http://127.0.0.1:9/mcp"})
    assert listing.held_for_setup("fixture-signin", _proxied_error())
    # A connector that needs nothing is not held: its failure is a real failure.
    assert not listing.held_for_setup("fixture-plain", _proxied_error())
    assert not listing.held_for_setup("no-such-connector", _proxied_error())


def test_a_session_keeps_the_last_tools_when_a_proxy_drops_the_sign_in_type(
    fixture_catalog: Path,
) -> None:
    state.update("fixture-signin", enabled=True, values={"address": "http://127.0.0.1:9/mcp"})
    listing.remember(
        "fixture-signin", [Tool(name="fixture_status", inputSchema={"type": "object"})]
    )
    executor = _executor()
    spec = SimpleNamespace(connector="fixture-signin")
    kept = mcp_readiness._connector_tools_kept(executor, "fixture-signin", spec, _proxied_error())
    assert kept is not None and list(kept) == ["fixture-signin_fixture_status"]
    assert list(executor.merged) == ["fixture-signin_fixture_status"]
    # A ready connector's failed listing is not papered over with the old tools.
    listing.remember("fixture-plain", [Tool(name="ping", inputSchema={"type": "object"})])
    plain = SimpleNamespace(connector="fixture-plain")
    assert (
        mcp_readiness._connector_tools_kept(_executor(), "fixture-plain", plain, _proxied_error())
        is None
    )
