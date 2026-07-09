"""Regression tests for the shared ``ProviderHandshake`` engine (#772).

A single unparseable model row must not sink the whole handshake, but the drop
must not be silent either: the engine emits a structured
``reason=model_row_discovery_failed`` warning so a dropped row reaches the logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    ModelProfile,
)


class _StubHandshake(ProviderHandshake):
    """Minimal engine: one good row, one row whose config build explodes."""

    async def _open_client(self, ctx: HandshakeContext) -> Any:  # no network
        return object()

    async def _close_client(self, client: Any) -> None:
        return None

    async def check_connectivity(
        self, client: Any, ctx: HandshakeContext
    ) -> ConnectivityResult:
        return ConnectivityResult(ConnectivityState.OK, AuthState.OK)

    async def discover_models(
        self, client: Any, ctx: HandshakeContext
    ) -> list[dict[str, Any]]:
        return [{"id": "good-model"}, {"id": "broken-model"}]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        if raw.get("id") == "broken-model":
            raise ValueError("bad row: boom")
        return ModelProfile(id=str(raw["id"]))


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
        async def discover_models(
            self, client: Any, ctx: HandshakeContext
        ) -> list[dict[str, Any]]:
            return [{"id": "a"}, {"id": "b"}]

    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.handshake.base"):
        report = asyncio.run(_AllGood(provider=None).handshake(_ctx()))

    assert [m.id for m in report.models] == ["a", "b"]
    assert not [
        r for r in caplog.records if "model_row_discovery_failed" in r.getMessage()
    ]
