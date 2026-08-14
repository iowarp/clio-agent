"""``POST /v1/providers/models/refresh`` — the #1211 model-catalog refresh action.

Thin FastAPI registrar over :mod:`clio_agent.providers.model_discovery` (the sole
owner of every discovery mechanism + the overlay persistence). Kept as its own
router-factory module rather than grown into ``routes/providers.py`` — that file
is already at its #775 file-size ratchet; this route's entire reason for living
here is to add the capability without regrowing it (no-accretion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_provider_models_refresh_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register ``POST /v1/providers/models/refresh`` on ``app``.

    ``deps`` is accepted (unused) for signature consistency with every other
    ``register_<concern>_routes(app, deps)`` factory in :mod:`clio_agent.gact.routes`
    — this route needs nothing beyond the provider catalog + the discovery module,
    both reached as leaf imports.
    """

    @app.post("/v1/providers/models/refresh")
    async def refresh_provider_models() -> dict[str, Any]:
        """Probe every catalog provider for its CURRENT model list (#1211).

        Runs codex's ``model/list`` app-server RPC, claude_code's per-alias
        probe-validation, and the existing live handshake for HTTP backends —
        concurrently, one typed result per provider. A probe failure for one
        provider NEVER clears its previously-discovered list (no-silent-
        fallback): the response's per-row ``failed_reason`` says exactly which
        providers kept their prior overlay and why, while every provider's fresh
        ``added``/``removed``/``unchanged`` model-id delta rides along for the
        ``/update-models`` skill (:mod:`clio_agent.gact.builtin_skills`) to relay
        verbatim — no client-side comparison logic needed.
        """
        from clio_agent.providers import model_discovery  # noqa: PLC0415

        results = await model_discovery.refresh_all()
        return {"results": results}


__all__ = ["register_provider_models_refresh_routes"]
