"""LM Studio provider handshake.

LM Studio exposes an OpenAI-compatible API under ``{api_base}`` (e.g.
``http://host:1234/v1``) plus a richer, native ``/api/v0`` surface served from
the same host root (``http://host:1234``). The native ``/api/v0/models``
endpoint is what makes LM Studio worth a bespoke handshake: it self-reports
``max_context_length`` (the model's own ceiling — a **model** fact),
``loaded_context_length`` (the *runtime* window an already-loaded model is
actually serving — a **deployment** fact, brief Part 6), the ``quantization``/
``arch`` of the GGUF, the load ``state``, and a ``capabilities`` list
(``"tool_use"`` / ``"vision"`` => native tool calling / image input, both
**model** facts).

LM Studio is a local backend with no authentication, so the connectivity probe
reports :data:`AuthState.NOT_REQUIRED`. The probe hits the native endpoint first
and falls back to the OpenAI-compatible ``{api_base}/models`` so that a stripped
build (or an older LM Studio) still registers as reachable.
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
    """Handshake for a local LM Studio backend (no auth, native ``/api/v0``)."""

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Probe LM Studio; native ``/api/v0/models`` first, OpenAI ``/models`` fallback.

        Either endpoint answering marks the backend reachable. LM Studio requires
        no credential, so auth is always :data:`AuthState.NOT_REQUIRED`.
        """
        root = native_root(ctx.api_base)
        base = ctx.api_base.rstrip("/")
        urls = (f"{root}/api/v0/models", f"{base}/models")
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
        """Return the raw rows from ``{root}/api/v0/models`` (the ``data`` array)."""
        root = native_root(ctx.api_base)
        response = await client.get(f"{root}/api/v0/models")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Build a :class:`DiscoveredModelFacts` from one ``/api/v0`` model row.

        Maps LM Studio's self-reported fields: ``max_context_length`` ->
        ``ModelCapabilities.context_max`` (the ceiling), ``loaded_context_length``
        -> ``DeploymentCapabilities.context_served`` (the runtime window),
        ``quantization``/``arch`` pass through as raw identity metadata, the
        ``capabilities`` list becomes ``ModelCapabilities.tools``/
        ``input_modalities``, and ``state == "loaded"`` sets ``is_loaded``.
        """
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
