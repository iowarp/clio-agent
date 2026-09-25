"""LM Studio provider handshake.

LM Studio 0.4+ exposes a newer native ``GET /api/v1/models`` surface
(``max_context_length``, ``loaded_context_length``, ``capabilities.vision``,
``trained_for_tool_use``, ``reasoning.allowed_options`` -- brief Part 6, parsed
by :mod:`clio_agent.providers.capabilities.dialects.lm_studio`); older builds
only have ``GET /api/v0/models`` (``capabilities`` as a flat string list). Both
live at the NATIVE host root (``http://host:1234``), not under the
OpenAI-compatible ``{api_base}`` (``http://host:1234/v1``).

:meth:`discover_models` tries v1 first, falling back to v0
(:func:`~clio_agent.providers.capabilities.dialects.lm_studio.fetch_rows`), and
tags each raw row with which schema answered so :meth:`discover_model_config`
knows which field mapping applies -- v1's own parser
(:func:`~clio_agent.providers.capabilities.dialects.lm_studio.parse_v1_row`),
or this class's own v0 mapping below.

LM Studio is a local backend with no authentication, so the connectivity probe
reports :data:`AuthState.NOT_REQUIRED`. The probe hits v1, then v0, then falls
back to the OpenAI-compatible ``{api_base}/models`` so that a stripped build
(or an older LM Studio) still registers as reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.api_base import native_root
from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
    unknown,
)
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class LMStudioHandshake(ProviderHandshake):
    """Handshake for a local LM Studio backend (no auth, native v1 with a v0 fallback)."""

    #: Both the v1 and v0 model rows report per-model capability evidence
    #: (v1: ``capabilities.vision``/``trained_for_tool_use``; v0: a flat
    #: ``capabilities`` list), so this backend really can evidence input
    #: modalities either way.
    reports_input_modalities = True

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Probe LM Studio; native v1, then v0, then the OpenAI ``/models`` fallback.

        Any endpoint answering marks the backend reachable. LM Studio requires
        no credential, so auth is always :data:`AuthState.NOT_REQUIRED`.
        """
        root = native_root(ctx.api_base)
        base = ctx.api_base.rstrip("/")
        urls = (f"{root}/api/v1/models", f"{root}/api/v0/models", f"{base}/models")
        last_error: str | None = None
        for url in urls:
            try:
                response = await client.get(url)
            except Exception as exc:  # network/DNS/connection failure  # noqa: BLE001 - connection failure captured in last_error
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code < 400:
                return ConnectivityResult(
                    connectivity=ConnectivityState.OK,
                    auth=AuthState.NOT_REQUIRED,
                )
            last_error = f"{url} -> HTTP {response.status_code}"
        return ConnectivityResult(
            connectivity=ConnectivityState.UNREACHABLE,
            auth=AuthState.NOT_REQUIRED,
            error=last_error or "LM Studio unreachable",
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return the raw rows from ``/api/v1/models``, falling back to ``/api/v0/models``.

        Each row is tagged with ``_lm_studio_schema`` (``"v1"``/``"v0"``) so
        :meth:`discover_model_config` knows which field mapping applies.
        """
        from clio_agent.providers.capabilities.dialects import (  # noqa: PLC0415
            lm_studio as lm_studio_dialect,
        )

        rows, schema = await lm_studio_dialect.fetch_rows(client, ctx.api_base)
        if not schema:
            raise RuntimeError("lm studio: neither /api/v1/models nor /api/v0/models answered")
        for row in rows:
            row["_lm_studio_schema"] = schema
        return rows

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Build a :class:`DiscoveredModelFacts` from one LM Studio model row.

        Dispatches on the ``_lm_studio_schema`` tag :meth:`discover_models` set:
        a v1 row goes straight through
        :func:`~clio_agent.providers.capabilities.dialects.lm_studio.parse_v1_row`
        (brief Part 6's newer field names, including ``reasoning.allowed_options``);
        a v0 row keeps this class's own mapping below (``max_context_length`` ->
        the ceiling, ``loaded_context_length`` -> the runtime window,
        ``quantization``/``arch`` as raw identity metadata, the flat
        ``capabilities`` list -> tools/vision, ``state == "loaded"`` -> ``is_loaded``).
        """
        if raw.get("_lm_studio_schema") == "v1":
            from clio_agent.providers.capabilities.dialects import (  # noqa: PLC0415
                lm_studio as lm_studio_dialect,
            )

            model, deployment = lm_studio_dialect.parse_v1_row(
                raw, provider_id=ctx.provider_id, api_base=ctx.api_base
            )
            discovered = DiscoveredModel(
                id=deployment.model_id,
                is_loaded=deployment.context_served.known,
                raw={k: v for k, v in raw.items() if k != "_lm_studio_schema"},
            )
            return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)

        capabilities = raw.get("capabilities") or []
        if not isinstance(capabilities, list):
            capabilities = []
        caps = tuple(str(cap) for cap in capabilities)
        model_id = str(raw.get("id", ""))
        observed_at = _now_iso()
        context_max = _positive_int(raw.get("max_context_length"))
        capabilities_known = bool(caps)
        model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
        model_key = model_key_fact.value or model_id

        model = ModelCapabilities(
            model_key=model_key,
            context_max=(
                Fact(
                    value=context_max,
                    source="server_report",
                    observed_at=observed_at,
                    detail="lmstudio /api/v0/models max_context_length",
                )
                if context_max is not None
                else unknown()
            ),
            tools=(
                Fact(
                    value="tool_use" in caps,
                    source="server_report",
                    observed_at=observed_at,
                    detail="lmstudio /api/v0/models capabilities",
                )
                if capabilities_known
                else unknown()
            ),
            input_modalities=(
                Fact(
                    value=modalities_from_capabilities(caps),
                    source="server_report",
                    observed_at=observed_at,
                    detail="lmstudio /api/v0/models capabilities",
                )
                if capabilities_known
                else unknown()
            ),
        )
        loaded_context = _positive_int(raw.get("loaded_context_length"))
        deployment = DeploymentCapabilities(
            provider_id=ctx.provider_id,
            api_base=ctx.api_base,
            model_id=model_id,
            model_key=model_key_fact,
            context_served=(
                Fact(
                    value=loaded_context,
                    source="server_report",
                    observed_at=observed_at,
                    detail="lmstudio /api/v0/models loaded_context_length",
                )
                if loaded_context is not None
                else unknown()
            ),
        )
        discovered = DiscoveredModel(
            id=model_id,
            is_loaded=raw.get("state") == "loaded",
            raw={
                **dict(raw),
                "quantization": raw.get("quantization"),
                "arch": raw.get("arch"),
                "capabilities": list(caps),
            },
        )
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)
