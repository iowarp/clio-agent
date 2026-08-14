"""codex model-catalog discovery: the real ``model/list`` app-server RPC
(iowarp/clio-agent#1211)."""

from __future__ import annotations

from clio_agent.providers.model_discovery.overlay import (
    CODEX_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)


def discover_codex(*, timeout: float = 20.0) -> ProviderDiscoveryResult:
    """Refresh codex's live model catalog via the app-server's ``model/list`` RPC.

    Verified live 2026-08-14 against codex-cli 0.147.0: the account's REAL current
    catalog (``gpt-5.6-sol`` default, ``gpt-5.6-terra``, ``gpt-5.6-luna``,
    ``gpt-5.5``, ``gpt-5.4``, ``gpt-5.4-mini``, ``gpt-5.3-codex-spark``) — none of
    which is ``gpt-5.5-codex``/``gpt-5.1`` (#1184's stale, rejected pins).
    Synchronous+blocking (the app-server bridge's JSON-RPC call is a blocking
    socket read); callers on the async refresh path wrap this in
    ``asyncio.to_thread``. Spawns a DEDICATED, ONE-OFF app-server process
    (``model="", cwd=None`` — ``model/list`` needs no ``thread/start``) and closes
    it immediately after listing (#1211 review R4) — this is a rare, explicit
    refresh action, not a warm process meant to be reused across calls; leaving
    it in the pool forever would silently accumulate one extra idle subprocess
    per refresh-triggering process lifetime.
    """
    from clio_agent.providers.codex_app_server import _APP_SERVER_POOL, CodexAppServerError
    from clio_agent.providers.codex_litellm import (  # noqa: PLC0415
        CodexCLIUnavailableError,
        _resolve_codex_binary,
    )

    try:
        binary = _resolve_codex_binary()
    except CodexCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    process = _APP_SERVER_POOL.process_for(binary=binary, model="", cwd=None)
    try:
        rows = process.list_models(timeout=timeout)
    except CodexAppServerError as exc:
        return ProviderDiscoveryResult(
            provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
        )
    finally:
        process.close()  # one-off discovery process -- never left warm in the pool
    discovered = [
        {
            "id": str(m.get("id") or ""),
            "name": str(m.get("displayName") or m.get("id") or ""),
            "description": str(m.get("description") or ""),
        }
        for m in rows
        if isinstance(m, dict) and m.get("id")
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason="codex app-server model/list returned zero models",
        )
    default_model = next(
        (str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("isDefault")),
        discovered[0]["id"],
    )
    discovered = attach_context_limits(discovered, "codex")
    return ProviderDiscoveryResult(
        provider="codex", discovered=discovered, source=CODEX_SOURCE, default_model=default_model
    )


__all__ = ["discover_codex"]
