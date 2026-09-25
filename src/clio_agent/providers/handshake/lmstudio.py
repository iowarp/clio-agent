"""LM Studio handshake — thin orchestration over :mod:`clio_agent.providers.capabilities.dialects.lm_studio`.

Every HTTP read of LM Studio's native surface (``/api/v1/models`` with an
``/api/v0/models`` fallback) and every mapping of its fields onto the
capability records lives in
:mod:`clio_agent.providers.capabilities.dialects.lm_studio` -- the single
place that talks to an LM Studio server (consolidation review of #1447: this
class used to hold its own v0 field mapping alongside the dialect module's).
This class owns only connectivity dispatch and the per-phase plumbing.

LM Studio is a local backend with no authentication, so the connectivity probe
reports :data:`AuthState.NOT_REQUIRED`. It hits the SAME v1-then-v0 dialect
read discovery already needs (so a reachable server never costs two fetches to
find that out) and, only if NEITHER native endpoint answers, falls back to the
generic OpenAI-compatible ``{api_base}/models`` reachability check (not
LM-Studio-specific, so no dialect delegation needed) for a stripped build.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.capabilities.dialects import lm_studio as lm_studio_dialect
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


class LMStudioHandshake(ProviderHandshake):
    """Handshake for a local LM Studio backend (no auth, native v1 with a v0 fallback)."""

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Reachable if either native endpoint answers; else the generic OpenAI fallback."""
        _rows, schema = await lm_studio_dialect.fetch_rows(client, ctx.api_base)
        if schema:
            return ConnectivityResult(connectivity=ConnectivityState.OK, auth=AuthState.NOT_REQUIRED)
        try:
            response = await client.get(f"{ctx.api_base.rstrip('/')}/models")
        except Exception as exc:  # noqa: BLE001 - connection failure captured in the result
            return ConnectivityResult(
                connectivity=ConnectivityState.UNREACHABLE,
                auth=AuthState.NOT_REQUIRED,
                error=f"LM Studio unreachable: {type(exc).__name__}: {exc}",
            )
        if response.status_code < 400:
            return ConnectivityResult(connectivity=ConnectivityState.OK, auth=AuthState.NOT_REQUIRED)
        return ConnectivityResult(
            connectivity=ConnectivityState.UNREACHABLE,
            auth=AuthState.NOT_REQUIRED,
            error=f"LM Studio unreachable: HTTP {response.status_code}",
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return the raw rows from ``/api/v1/models``, falling back to ``/api/v0/models``.

        Each row is tagged with ``_lm_studio_schema`` (``"v1"``/``"v0"``) so
        :meth:`discover_model_config` knows which dialect parser applies.
        """
        rows, schema = await lm_studio_dialect.fetch_rows(client, ctx.api_base)
        if not schema:
            raise RuntimeError("lm studio: neither /api/v1/models nor /api/v0/models answered")
        for row in rows:
            row["_lm_studio_schema"] = schema
        return rows

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Dispatch to the dialect parser matching the schema :meth:`discover_models` tagged."""
        schema = raw.pop("_lm_studio_schema", "v0")
        parse = lm_studio_dialect.parse_v1_row if schema == "v1" else lm_studio_dialect.parse_v0_row
        model, deployment = parse(raw, provider_id=ctx.provider_id, api_base=ctx.api_base)
        discovered = DiscoveredModel(
            id=deployment.model_id,
            is_loaded=self._is_loaded(raw, schema, deployment),
            raw=dict(raw),
        )
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)

    @staticmethod
    def _is_loaded(raw: dict[str, Any], schema: str, deployment: Any) -> bool:
        """Whether this row is the currently-loaded model, per each schema's own signal."""
        if schema == "v0":
            return raw.get("state") == "loaded"
        return bool(deployment.context_served.known)
