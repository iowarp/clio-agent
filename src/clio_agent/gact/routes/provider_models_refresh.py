"""``POST /v1/providers/models/refresh`` — the #1211 model-catalog refresh action.

Thin FastAPI registrar over :mod:`clio_agent.providers.model_discovery` (the sole
owner of every discovery mechanism + the overlay persistence). Kept as its own
router-factory module rather than grown into ``routes/providers.py`` — that file
is already at its #775 file-size ratchet; this route's entire reason for living
here is to add the capability without regrowing it (no-accretion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

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
    async def refresh_provider_models(request: Request) -> dict[str, Any]:
        """Probe every CONFIGURED provider for its CURRENT model list (#1211).

        With no body (or an empty one), scans the catalog and probes only the
        providers :func:`~clio_agent.providers.model_discovery.is_provider_configured`
        reports ready — auth/CLI present (#1211 review R2). An optional
        ``{"providers": ["codex", "openai", ...]}`` body narrows (or widens past
        the configured-only filter) the scan to exactly those preset ids
        (#1211 review R3); an unknown id in that list is a typed 404.

        Runs the Codex SDK model catalog, claude_code's per-alias
        probe-validation, and the existing live handshake for HTTP backends —
        concurrently, one typed result per provider, each capped at its own
        deadline so a wedged probe can never hang the whole action. A probe
        failure for one provider NEVER clears its previously-discovered list
        (no-silent-fallback): the response's per-row ``failed_reason`` says
        exactly which providers kept their prior overlay and why, while every
        provider's fresh ``added``/``removed``/``unchanged`` model-id delta
        rides along for the ``/update-models`` skill
        (:mod:`clio_agent.gact.builtin_skills`) or the ``refresh_provider_models``
        agent tool's caller to relay verbatim — no client-side comparison logic
        needed.
        """
        from clio_agent.gact.routes._body import json_body  # noqa: PLC0415
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        body = await json_body(request, route="POST /v1/providers/models/refresh")
        requested = body.get("providers")
        presets = None
        if isinstance(requested, list) and requested:
            presets = []
            unknown: list[str] = []
            for provider_id in requested:
                preset = get_provider(str(provider_id))
                if preset is None:
                    unknown.append(str(provider_id))
                else:
                    presets.append(preset)
            if unknown:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"unknown provider id(s): {unknown}",
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )

        results = await model_discovery.refresh_all(presets=presets)
        return {"results": results}


__all__ = ["register_provider_models_refresh_routes"]
