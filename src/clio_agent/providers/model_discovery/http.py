"""HTTP-backed provider model-catalog discovery: reuse the existing live
handshake path (iowarp/clio-agent#1211)."""

from __future__ import annotations

from clio_agent.providers.catalog import Provider
from clio_agent.providers.model_discovery.overlay import HTTP_SOURCE, ProviderDiscoveryResult


async def discover_http(preset: Provider, *, api_key: str) -> ProviderDiscoveryResult:
    """Refresh an HTTP-backed provider's catalog via the existing live handshake path.

    Reuses :func:`clio_agent.providers.handshake.run_handshake` — the SAME
    mechanism ``GET /v1/providers/{id}/models`` already calls — with
    ``force=True``: a refresh explicitly bypasses the handshake TTL cache. The
    RESULT is still persisted to the overlay (so the refresh response's
    added/removed/unchanged delta is meaningful for HTTP providers too), but
    ``GET /v1/providers/{id}/models`` never SERVES an HTTP provider's overlay
    entry ahead of a fresh live handshake — that scoping is CLI-kinds-only
    (#1211 review D5); this function's job is only to populate it.
    """
    from clio_agent.providers.handshake import HandshakeContext, run_handshake  # noqa: PLC0415

    ctx = HandshakeContext(
        provider_id=preset.id,
        provider_kind=preset.provider_kind,
        api_base=preset.api_base,
        api_key=api_key,
        auth_mode="passive",
        allow_external_sources=True,
    )
    report = await run_handshake(ctx, force=True)
    wire = report.to_models_wire()
    models = wire.get("models") or []
    if not models:
        reason = report.error or (
            f"connectivity={report.connectivity.value} auth={report.auth.value}"
        )
        return ProviderDiscoveryResult(
            provider=preset.id, discovered=[], source=HTTP_SOURCE, failed_reason=reason
        )
    discovered = [
        {"id": str(m["id"]), "name": str(m.get("name") or m["id"]), "description": ""}
        for m in models
        if m.get("id")
    ]
    return ProviderDiscoveryResult(provider=preset.id, discovered=discovered, source=HTTP_SOURCE)


__all__ = ["discover_http"]
