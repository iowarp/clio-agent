"""Regression tests for the shared ``ProviderHandshake`` engine (#772).

A single unparseable model row must not sink the whole handshake, but the drop
must not be silent either: the engine emits a structured
``reason=model_row_discovery_failed`` warning so a dropped row reaches the logs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import httpx
import pytest

from clio_agent.providers.capabilities.records import DeploymentCapabilities, ModelCapabilities
from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    DiscoveredModelFacts,
)


class _StubHandshake(ProviderHandshake):
    """Minimal engine: one good row, one row whose config build explodes."""

    async def _open_client(self, ctx: HandshakeContext) -> Any:  # no network
        return object()

    async def _close_client(self, client: Any) -> None:
        return None

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        return ConnectivityResult(ConnectivityState.OK, AuthState.OK)

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        return [{"id": "good-model"}, {"id": "broken-model"}]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        if raw.get("id") == "broken-model":
            raise ValueError("bad row: boom")
        model_id = str(raw["id"])
        return DiscoveredModelFacts(
            discovered=DiscoveredModel(id=model_id),
            model=ModelCapabilities(model_key=model_id),
            deployment=DeploymentCapabilities(
                provider_id=ctx.provider_id, api_base=ctx.api_base, model_id=model_id
            ),
        )


def _ctx() -> HandshakeContext:
    return HandshakeContext(
        provider_id="stub",
        provider_kind="openai_compat",
        api_base="http://127.0.0.1:0",
        # keep enrich_capabilities offline so the test never touches the network
        allow_external_sources=False,
    )


def test_bad_model_row_is_dropped_and_warned(caplog) -> None:
    engine = _StubHandshake(provider=None)
    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.handshake.base"):
        report = asyncio.run(engine.handshake(_ctx()))

    # The good model survives; the broken one is dropped, not fatal.
    assert [m.id for m in report.models] == ["good-model"]
    assert report.connectivity == ConnectivityState.OK

    # The drop is surfaced with a structured reason, the model id, and the error.
    records = [r.getMessage() for r in caplog.records]
    matching = [m for m in records if "reason=model_row_discovery_failed" in m]
    assert matching, f"expected a model_row_discovery_failed warning, got: {records}"
    assert "broken-model" in matching[0]
    assert "bad row: boom" in matching[0]


def test_all_rows_valid_emits_no_drop_warning(caplog) -> None:
    class _AllGood(_StubHandshake):
        async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
            return [{"id": "a"}, {"id": "b"}]

    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.handshake.base"):
        report = asyncio.run(_AllGood(provider=None).handshake(_ctx()))

    assert [m.id for m in report.models] == ["a", "b"]
    assert not [r for r in caplog.records if "model_row_discovery_failed" in r.getMessage()]


def test_certificate_initialization_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog client setup preserves TLS/timeouts without blocking other API work."""
    loop_thread = threading.get_ident()
    actual_client = httpx.AsyncClient
    construction_threads: list[int] = []

    def construct(**kwargs: Any) -> httpx.AsyncClient:
        construction_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return actual_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", construct)

    async def run() -> None:
        engine = _StubHandshake(provider=None)
        client = await ProviderHandshake._open_client(engine, _ctx())
        try:
            assert client.timeout.connect == engine.timeout_connect
            assert client.timeout.read == max(engine.timeout_models, engine.timeout_model_config)
            assert client._transport._pool._ssl_context.verify_mode != 0
        finally:
            await ProviderHandshake._close_client(engine, client)

    asyncio.run(run())
    assert len(construction_threads) == 1


def test_capability_catalog_keeps_the_event_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Health requests can run while a cold catalog resolves endpoint facts."""
    from clio_agent.providers.capabilities import endpoint, invalidation
    from clio_agent.providers.identity import endpoint_key

    actual_build = endpoint.build_endpoint_capabilities
    entered = threading.Event()
    release = threading.Event()

    def slow_catalog(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(timeout=3), "capability catalog blocked the event loop"
        return actual_build(*args, **kwargs)

    monkeypatch.setattr(endpoint, "build_endpoint_capabilities", slow_catalog)

    async def run() -> None:
        engine = _StubHandshake(provider=None)
        pending = asyncio.create_task(engine.handshake(_ctx()))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            release.set()
            report = await pending
            assert report.connectivity == ConnectivityState.OK
            caps = invalidation.get_endpoint_capabilities(endpoint_key("stub", _ctx().api_base))
            assert caps is not None
        finally:
            release.set()

    asyncio.run(run())


def test_handshake_persists_live_report_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live limits still reach the DB without synchronous disk work on the loop."""
    from clio_agent.providers import handshake
    from clio_agent.providers.handshake.sources import db

    actual_record = db.record_report
    loop_thread = threading.get_ident()
    recorded: list[Any] = []

    def record(report: Any) -> None:
        assert threading.get_ident() != loop_thread
        actual_record(report)
        recorded.append(report)

    monkeypatch.setattr(db, "record_report", record)
    monkeypatch.setattr(handshake, "get_handshake_for", lambda *args: _StubHandshake(None))
    report = asyncio.run(handshake.run_handshake(_ctx(), force=True))
    assert report.connectivity == ConnectivityState.OK
    assert recorded == [report]
