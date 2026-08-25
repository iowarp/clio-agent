"""Codex model discovery through the official Python SDK."""

from __future__ import annotations

import asyncio
from typing import Any

from openai_codex import AsyncCodex, CodexConfig, CodexError

from clio_agent.providers.codex_stream import BARE_LM_CONFIG_OVERRIDES, IsolatedCodexHome
from clio_agent.providers.model_discovery.overlay import (
    CODEX_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)


def discover_codex(*, timeout: float = 20.0) -> ProviderDiscoveryResult:
    """Refresh the account's live Codex catalog through ``openai_codex``.

    The Python SDK owns its pinned runtime and authentication. CLIO neither
    resolves a ``codex`` executable nor opens an app-server protocol connection.
    """

    async def _query() -> Any:
        sdk_home = IsolatedCodexHome()
        try:
            async with AsyncCodex(
                CodexConfig(
                    config_overrides=BARE_LM_CONFIG_OVERRIDES,
                    env=sdk_home.start(),
                )
            ) as client:
                return await client.models()
        finally:
            sdk_home.close()

    async def _bounded_query() -> Any:
        return await asyncio.wait_for(_query(), timeout=timeout)

    try:
        response = asyncio.run(_bounded_query())
    except (CodexError, OSError, RuntimeError, TimeoutError) as exc:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=f"Codex Python SDK model discovery failed: {exc}",
        )

    rows = list(response.data)
    discovered = [
        {
            "id": str(row.id),
            "name": str(row.display_name or row.id),
            "description": str(row.description or ""),
        }
        for row in rows
        if row.id
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason="Codex Python SDK returned zero models",
        )
    default_model = next((str(row.id) for row in rows if row.is_default), discovered[0]["id"])
    return ProviderDiscoveryResult(
        provider="codex",
        discovered=attach_context_limits(discovered, "codex"),
        source=CODEX_SOURCE,
        default_model=default_model,
    )


__all__ = ["discover_codex"]
