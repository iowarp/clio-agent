"""Per-provider async handshake protocol.

A handshake answers, for one provider: can I reach + authenticate, what models
does it serve, and what is each model's real config (context window, reasoning/
tool capabilities). Results feed clio's runtime config (``max_tokens`` sizing,
LM Studio load-sizing, capability decisions) and surface in ``/v1/health`` and
the model picker.

Public API:
    run_handshake(ctx)            -> HandshakeReport   (async, cached)
    run_handshake_sync(ctx)       -> HandshakeReport   (sync bridge for config.py)
    get_handshake_for(kind)       -> ProviderHandshake (registry-keyed dispatch)
    handshake_mcp_servers(specs)  -> list[MCPServerReport]  (the +MCP scope)
    resolve_context(id, kind)     -> (window, source)  (the context-source factory)
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.handshake import cache
from clio_agent.providers.handshake.argonne import ArgonneHandshake
from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    DiscoveryAuthRejected,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.cli_catalog import (
    ClaudeCodeCatalogHandshake,
    CliCatalogHandshake,
    CodexCatalogHandshake,
)
from clio_agent.providers.handshake.lmstudio import LMStudioHandshake
from clio_agent.providers.handshake.mcp import MCPServerReport, handshake_mcp_servers
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
)
from clio_agent.providers.handshake.noop import NoOpHandshake
from clio_agent.providers.handshake.ollama import OllamaHandshake
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake
from clio_agent.providers.handshake.sources import resolve_context

__all__ = [
    "ProviderHandshake",
    "HandshakeContext",
    "ConnectivityResult",
    "DiscoveryAuthRejected",
    "HandshakeReport",
    "DiscoveredModel",
    "ConnectivityState",
    "AuthState",
    "MCPServerReport",
    "CodexCatalogHandshake",
    "CliCatalogHandshake",
    "ClaudeCodeCatalogHandshake",
    "NoOpHandshake",
    "handshake_mcp_servers",
    "resolve_context",
    "get_handshake_for",
    "run_handshake",
    "run_handshake_sync",
]

# provider_kind -> handshake class. Unknown kinds fall through to the
# OpenAI-compatible handshake, so a new openai-compat provider needs only a
# registry row (zero handshake code).
_BY_KIND: dict[str, type[ProviderHandshake]] = {
    "argonne": ArgonneHandshake,
    "lm_studio": LMStudioHandshake,
    "openai": OpenAICompatHandshake,
    "anthropic": OpenAICompatHandshake,
    "ollama": OllamaHandshake,
    "vllm": OpenAICompatHandshake,
    "openrouter": OpenAICompatHandshake,
    # #1211: codex/claude_code prefer the refresh-overlay-aware variant (still
    # zero network calls on this passive read path); NoOpHandshake stays the
    # registry entry for any OTHER future no-HTTP-surface CLI provider.
    "codex": CodexCatalogHandshake,
    "claude_code": ClaudeCodeCatalogHandshake,
}


def get_handshake_for(provider_kind: str, provider: Any = None) -> ProviderHandshake:
    """Return the handshake for a ``provider_kind`` (defaults to OpenAI-compatible)."""
    cls = _BY_KIND.get(provider_kind, OpenAICompatHandshake)
    return cls(provider)


async def run_handshake(
    ctx: HandshakeContext,
    *,
    provider: Any = None,
    force: bool = False,
    ttl_s: float = cache.DEFAULT_TTL_S,
) -> HandshakeReport:
    """Run (or return a cached) handshake for ``ctx``. Never raises.

    On a fresh (non-cached) run, live-discovered limits are recorded into the local
    model-limits DB (and disagreements logged), so the cascade learns over time.

    ``provider`` defaults to the registry row for ``ctx.provider_id``: the
    handshake resolves its endpoint DIALECT from that row's ``litellm_prefix``
    (``provider_kind`` alone collapses OpenRouter, a self-hosted vLLM server and
    every cloud OpenAI-compatible API onto ``"openai"``). Without it an
    OpenRouter probe fetched the default text-only listing (458 of 628 models)
    and never ran the OpenRouter adapter. A custom endpoint with no registry row
    keeps ``None`` and resolves from its kind, as before.
    """
    if provider is None:
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        provider = get_provider(ctx.provider_id)
    handshake = get_handshake_for(ctx.provider_kind, provider)
    key = cache.cache_key(ctx.provider_id, ctx.api_base)

    async def _run_and_record() -> HandshakeReport:
        report = await handshake.handshake(ctx)
        from clio_agent.providers.handshake.sources import db  # noqa: PLC0415

        db.record_report(report)
        return report

    return await cache.cached_or_run(key, _run_and_record, ttl_s=ttl_s, force=force)


def run_handshake_sync(
    ctx: HandshakeContext,
    *,
    provider: Any = None,
    force: bool = False,
    ttl_s: float = cache.DEFAULT_TTL_S,
) -> HandshakeReport:
    """Synchronous wrapper for callers without an event loop (e.g. ``config.py``).

    Uses the established asyncio.run-or-threadpool bridge (mirrors
    ``runtime.status._list_gateway_capabilities``) so it is safe whether or not
    an event loop is already running on the calling thread.
    """
    import asyncio  # noqa: PLC0415
    import concurrent.futures  # noqa: PLC0415

    async def _go() -> HandshakeReport:
        return await run_handshake(ctx, provider=provider, force=force, ttl_s=ttl_s)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_go())).result()
