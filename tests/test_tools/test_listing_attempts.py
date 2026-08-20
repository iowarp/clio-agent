"""#1240: the discovery/listing ownership registry.

Force-closing an in-flight attempt (from another thread) must actually
unblock it and tear down its transport -- the exact mechanism that keeps an
abandoned attempt's spawned child from leaking forever (the CI-observed
child-process leak on the #1237 hotfix). Exercises the registry through the
REAL production entry point (``gateway._list_declared_tools``) with a fake
Client/transport standing in for a real stdio spawn that never answers, so
this pins the actual integration, not just the registry in isolation.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from clio_agent.tools import gateway as gateway_module
from clio_agent.tools import listing_attempts
from clio_agent.tools.gateway import _list_declared_tools
from clio_agent.tools.mcp_config import MCPServerSpec


class _HangingTransport:
    """Stands in for a real stdio transport whose spawned child never answers.

    ``disconnect()`` is what a real transport does to actually kill the
    spawned subprocess; here it just flips the event ``list_tools`` is
    parked on, so a test can prove the SAME code path production uses
    (``_list_declared_tools``'s ``finally``) is what force-close exercises.
    """

    def __init__(self) -> None:
        self.disconnect_calls = 0
        # Created eagerly (not lazily inside list_tools) so there is no window
        # where a force-close could race ahead of list_tools() starting to
        # wait on it: modern asyncio.Event binds to whichever loop first
        # awaits/sets it, so constructing it before any loop is running is
        # safe as long as both ends run on the SAME loop (true here: the
        # force-close coroutine is injected onto THIS attempt's own loop via
        # run_coroutine_threadsafe).
        self.closed_event = asyncio.Event()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.closed_event.set()


class _HangingClient:
    """Fake fastmcp ``Client``: ``list_tools()`` hangs until the transport closes.

    Mirrors the REAL constructor's ``timeout``/``init_timeout`` kwargs (accepted
    and ignored) so this is a drop-in for ``gateway.Client`` in
    ``_list_declared_tools`` without changing that function's call shape.
    """

    def __init__(
        self, transport: _HangingTransport, *, timeout: object = None, init_timeout: object = None
    ) -> None:
        del timeout, init_timeout
        self.transport = transport

    async def __aenter__(self) -> "_HangingClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def list_tools(self) -> list[Any]:
        await self.transport.closed_event.wait()
        raise RuntimeError("simulated: server never answered list_tools; force-closed")


def test_force_close_listing_attempt_unblocks_a_hung_list_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST for #1240 (the child-process leak): before this fix, an
    abandoned discovery attempt's connect/list call had no bound AND nothing
    ever closed its transport — the exact shape that leaked a real stdio
    child in CI. This proves the ownership registry actually works: a caller
    on ANOTHER thread can force-close a specific in-flight attempt and the
    hung ``list_tools()`` call unblocks via the SAME transport-teardown path
    production uses."""

    monkeypatch.setattr(gateway_module, "transport_for", lambda spec, cwd=None: _HangingTransport())
    monkeypatch.setattr(gateway_module, "Client", _HangingClient)

    spec = MCPServerSpec(name="hangs", transport="stdio", command="fake-launcher")
    attempt_key = object()
    outcome: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            _list_declared_tools(spec, timeout_s=None, attempt_key=attempt_key)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        deadline = time.time() + 5.0
        while attempt_key not in listing_attempts._attempts and time.time() < deadline:
            time.sleep(0.01)
        assert attempt_key in listing_attempts._attempts, "attempt never registered itself"

        closed = listing_attempts.force_close_listing_attempt(attempt_key)
        assert closed is True

        worker.join(timeout=5.0)
        assert not worker.is_alive(), "force_close_listing_attempt did not unblock the hung call"
    finally:
        worker.join(timeout=1.0)

    assert isinstance(outcome.get("error"), RuntimeError)
    assert attempt_key not in listing_attempts._attempts, "attempt not deregistered"


def test_force_close_listing_attempt_is_a_noop_for_an_unknown_key() -> None:
    assert listing_attempts.force_close_listing_attempt(object()) is False


def test_register_and_unregister_are_noops_for_a_none_key() -> None:
    """A caller that opts out of ownership tracking (``attempt_key=None``,
    e.g. ``list_tool_definitions``'s non-boot callers) must never pollute the
    registry."""

    listing_attempts.register(None, asyncio.new_event_loop(), object())
    assert listing_attempts._attempts == {}
    listing_attempts.unregister(None)  # must not raise


def test_force_close_all_closes_every_registered_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shutdown-symmetry sweep (#1240): ``ClioAgent.shutdown`` calls this
    to close whatever the healer/discovery pass left mid-connect."""

    monkeypatch.setattr(gateway_module, "transport_for", lambda spec, cwd=None: _HangingTransport())
    monkeypatch.setattr(gateway_module, "Client", _HangingClient)

    specs = {
        name: MCPServerSpec(name=name, transport="stdio", command="fake") for name in ("a", "b")
    }
    keys = [object(), object()]
    workers = [
        threading.Thread(
            target=lambda s=spec, k=key: _list_declared_tools(s, timeout_s=None, attempt_key=k),
            daemon=True,
        )
        for spec, key in zip(specs.values(), keys, strict=True)
    ]
    for w in workers:
        w.start()
    deadline = time.time() + 5.0
    while not all(k in listing_attempts._attempts for k in keys) and time.time() < deadline:
        time.sleep(0.01)
    assert all(k in listing_attempts._attempts for k in keys), "not every attempt registered"

    closed_count = listing_attempts.force_close_all()
    assert closed_count >= 2

    for w in workers:
        w.join(timeout=5.0)
        assert not w.is_alive()
    assert not any(k in listing_attempts._attempts for k in keys)
