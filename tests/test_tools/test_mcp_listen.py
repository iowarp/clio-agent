"""subscriptions/listen -> listing-cache invalidation (#1285, C1-S5 item 2).

fastmcp's ``Client`` has no ``.listen()`` (verified: no ``def listen`` under
``fastmcp/client/``) -- ``watch_list_changed`` drives the raw SDK
``mcp.client.subscriptions.listen(client.session, ...)`` directly.

**Verified library gap:** fastmcp's SERVER has zero ``subscriptions/listen``
support (grepped the whole installed ``fastmcp`` package for "Subscription":
no hits) -- a live call against the exerciser raises ``-32601 Method not
found``, proven by ``test_exerciser_server_does_not_implement_subscriptions_listen``
below. Reconfirmed unchanged across the #1285 C1-S5 item-5 fastmcp b1->b5
bump (this test is the regression lock -- if a future bump ever flips it, that
IS the signal to swap this module to ``watch_list_changed`` as the primary
path). So ``watch_list_changed`` is tested here against a FAKE session driving the real
SDK ``listen()`` machinery in-process (no server needed -- ``listen()`` talks
to ``session._dispatcher``/``session._register_listen_route``, which a bare
``ClientSession`` subclass provides without a live connection). The OTHER
signal, ``list_changed_message_handler``, is tested end-to-end against the
REAL exerciser, because fastmcp servers verifiably DO emit
``notifications/tools/list_changed`` unsolicited (see module docstring).
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import METHOD_NOT_FOUND

from clio_agent.tools import listing_cache
from clio_agent.tools.mcp_listen import (
    ListenUnsupported,
    list_changed_message_handler,
    watch_list_changed,
)
from tests.test_tools.mcp_exerciser import (
    EXERCISER_NAMESPACE,
    LIST_CHANGED_TOOL_NAME,
    build_exerciser_server,
)

# --------------------------------------------------------------------------- #
# The verified library gap, pinned as a regression lock                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_exerciser_server_does_not_implement_subscriptions_listen() -> None:
    """Regression lock for the finding driving this module's dual-path design.

    If this ever starts passing (a future fastmcp adds server-side listen
    support), ``watch_list_changed`` becomes live-testable against the real
    exerciser too -- update this test's expectation, don't delete it."""

    server = build_exerciser_server()
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc_info:
            from mcp.client.subscriptions import listen

            async with listen(client.session, tools_list_changed=True):
                pass
    assert exc_info.value.code == METHOD_NOT_FOUND


# --------------------------------------------------------------------------- #
# list_changed_message_handler: the path that DOES work today                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_changed_message_handler_invalidates_on_real_mutation(monkeypatch) -> None:
    server = build_exerciser_server()
    invalidated: list[str] = []
    monkeypatch.setattr(
        listing_cache,
        "invalidate_namespace",
        lambda namespace, **_: invalidated.append(namespace) or True,
    )

    handler = list_changed_message_handler(EXERCISER_NAMESPACE)
    async with Client(server, message_handler=handler) as client:
        await client.call_tool(LIST_CHANGED_TOOL_NAME, {})
        await asyncio.sleep(0.2)  # unsolicited notification delivery is async

    assert invalidated == [EXERCISER_NAMESPACE]


@pytest.mark.asyncio
async def test_list_changed_message_handler_ignores_other_notifications(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        listing_cache, "invalidate_namespace", lambda namespace, **_: calls.append(namespace)
    )
    handler = list_changed_message_handler("ns")
    from mcp_types import PromptListChangedNotification

    await handler(PromptListChangedNotification())
    assert calls == []


# --------------------------------------------------------------------------- #
# watch_list_changed: unit-level, driving the REAL function against a faked  #
# ``mcp.client.subscriptions.listen`` (real server-side listen support does  #
# not exist in this fastmcp version to test against live -- see above).      #
# ``watch_list_changed`` imports ``listen`` freshly inside its own body, so   #
# patching the module attribute is observed by the function under test.      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_watch_list_changed_maps_tools_list_changed_to_invalidation(monkeypatch) -> None:
    import mcp.client.subscriptions as subscriptions_module

    invalidated: list[str] = []
    monkeypatch.setattr(
        listing_cache,
        "invalidate_namespace",
        lambda namespace, **_: invalidated.append(namespace) or True,
    )

    class _FakeSubscription:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not hasattr(self, "_yielded"):
                self._yielded = True
                return subscriptions_module.ToolsListChanged()
            raise StopAsyncIteration

    class _FakeListenCtx:
        async def __aenter__(self):
            return _FakeSubscription()

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(subscriptions_module, "listen", lambda *a, **k: _FakeListenCtx())

    changes = 0

    async def _on_change() -> None:
        nonlocal changes
        changes += 1

    class _FakeClient:
        session = object()

    await watch_list_changed(_FakeClient(), "ns-fake", on_change=_on_change)

    assert invalidated == ["ns-fake"]
    assert changes == 1


@pytest.mark.asyncio
async def test_watch_list_changed_raises_typed_error_on_legacy_connection() -> None:
    server = build_exerciser_server()
    async with Client(server, mode="legacy") as client:
        with pytest.raises(ListenUnsupported) as exc_info:
            await watch_list_changed(client, EXERCISER_NAMESPACE)
        assert "2026-07-28" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# listing_cache.invalidate_namespace                                         #
# --------------------------------------------------------------------------- #


def test_invalidate_namespace_drops_only_matching_entries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "cache.json")
    from mcp.types import Tool

    tool = Tool(name="t", description="", inputSchema={"type": "object"})
    listing_cache.store_listing("ns-a", "python", ("a.py",), [tool])
    listing_cache.store_listing("ns-b", "python", ("b.py",), [tool])

    dropped = listing_cache.invalidate_namespace("ns-a")

    assert dropped is True
    assert listing_cache.load_listing("ns-a", "python", ("a.py",)) is None
    assert listing_cache.load_listing("ns-b", "python", ("b.py",)) is not None


def test_invalidate_namespace_is_false_when_nothing_cached(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "cache.json")
    assert listing_cache.invalidate_namespace("never-cached") is False
