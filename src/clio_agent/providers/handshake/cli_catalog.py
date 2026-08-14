"""``CliCatalogHandshake`` — :class:`NoOpHandshake` + the #1211 model-catalog overlay.

Extends :class:`~clio_agent.providers.handshake.noop.NoOpHandshake` (zero network
calls; codex/claude_code have no HTTP ``/models`` surface) so ``discover_models``
also consults the persisted refresh overlay
(:mod:`clio_agent.providers.model_discovery`) written by ``POST
/v1/providers/models/refresh`` — an explicit, user-triggered action.

This handshake NEVER re-runs discovery itself: the handshake's read path is hit on
every connect/doctor/model-picker-open, and a live CLI probe there would mean every
one of those pays a real (for claude_code, BILLED) round-trip. It only reads
whatever the last refresh wrote, falling back to the static registry catalog (the
:class:`NoOpHandshake` behavior) when no overlay entry exists yet (fresh install) —
this is what keeps the #740 guarantee (a CLI provider's models always resolve a
context window) intact regardless of whether a refresh has ever run.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.noop import NoOpHandshake


class CliCatalogHandshake(NoOpHandshake):
    """:class:`NoOpHandshake` variant whose model list prefers the refresh overlay."""

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return the overlay's discovered models when present, else the static catalog.

        A malformed on-disk overlay must not break this passive/ambient path (it
        runs on hot paths like connect/doctor, and the GACT server must always
        work — RULE 2); it degrades to the static catalog, same as a missing
        overlay. The corruption is still surfaced loudly on the diagnostic
        ``GET /v1/providers/{id}/models`` route and the refresh response, which
        read the overlay directly and let the error propagate.
        """
        from clio_agent.providers import model_discovery  # noqa: PLC0415

        try:
            wire = model_discovery.overlay_models_wire(ctx.provider_id, ctx.provider_kind)
        except model_discovery.OverlayMalformedError:
            wire = None
        if wire and wire.get("models"):
            return [
                {
                    "id": str(m.get("id") or ""),
                    "name": str(m.get("name") or ""),
                    "description": str(m.get("description") or ""),
                }
                for m in wire["models"]
                if isinstance(m, dict) and m.get("id")
            ]
        return await super().discover_models(client, ctx)


__all__ = ["CliCatalogHandshake"]
