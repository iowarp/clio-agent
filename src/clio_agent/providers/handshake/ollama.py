"""Ollama handshake — grounded on the Ollama REST API.

Ollama exposes two native endpoints we use (the OpenAI-compat ``/v1`` shim reports
none of this):

* ``GET /api/tags`` — installed models (``{"models": [{"model", "name", ...}]}``).
* ``POST /api/show`` ``{"model": id}`` — per-model metadata:
  - ``model_info["general.architecture"]`` (e.g. ``"qwen3"``) and, keyed by that
    arch, ``model_info["<arch>.context_length"]`` — the model's own maximum
    context (a **model** fact: brief Part 6 Ollama section, ``GET /v1/models``
    row);
  - ``capabilities`` — a list that includes ``"tools"`` (native function-calling)
    and ``"thinking"`` (reasoning) when the model supports them. This is an
    exhaustive, self-reported list (brief Part 4: Ollama ``capabilities`` is a
    **model** fact), so an ABSENT entry is real negative evidence, not "unknown".

So Ollama models resolve **live** with no catalog fallback needed for tools/
thinking. We inherit the keyless connectivity probe from
:class:`OpenAICompatHandshake` (Ollama needs no API key) and only override
discovery + per-model config to hit the native API.
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
    ThinkingSpec,
    modalities_from_capabilities,
    unknown,
)
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import DiscoveredModel, DiscoveredModelFacts
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaHandshake(OpenAICompatHandshake):
    """Ollama: list via ``/api/tags``, enrich each model via ``/api/show``."""

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List installed models from the native ``/api/tags``."""
        headers = self._auth_header(ctx)
        rows = await self._discover_ollama_tags(client, ctx, headers)
        return [r for r in rows if not self._is_embedding(r)]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Resolve one model's context window + capabilities via ``/api/show``."""
        model_id = str(raw.get("id") or raw.get("model") or "").strip()
        root = native_root(ctx.api_base)
        observed_at = _now_iso()

        context_window: int | None = None
        arch: str | None = None
        caps: tuple[str, ...] = ()
        try:
            resp = await client.post(f"{root}/api/show", json={"model": model_id})
            if resp.status_code < 400:
                data = resp.json()
                if isinstance(data, dict):
                    info = data.get("model_info")
                    if isinstance(info, dict):
                        raw_arch = info.get("general.architecture")
                        if isinstance(raw_arch, str) and raw_arch:
                            arch = raw_arch
                            window = info.get(f"{arch}.context_length")
                            if (
                                isinstance(window, int)
                                and not isinstance(window, bool)
                                and window > 0
                            ):
                                context_window = window
                    raw_caps = data.get("capabilities")
                    if isinstance(raw_caps, list):
                        caps = tuple(str(c) for c in raw_caps)
        except Exception:  # noqa: BLE001,S110 - /api/show best-effort; falls back to the enrich cascade
            # /api/show is best-effort: a failure leaves context_max/caps unknown
            # and the base enrich step falls back to the community-catalog cascade.
            pass

        capabilities_known = bool(caps)
        model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
        model_key = model_key_fact.value or model_id
        model = ModelCapabilities(
            model_key=model_key,
            context_max=(
                Fact(
                    value=context_window,
                    source="server_report",
                    observed_at=observed_at,
                    detail="ollama /api/show model_info.<arch>.context_length",
                )
                if context_window is not None
                else unknown()
            ),
            tools=(
                Fact(
                    value="tools" in caps,
                    source="server_report",
                    observed_at=observed_at,
                    detail="ollama /api/show capabilities",
                )
                if capabilities_known
                else unknown()
            ),
            input_modalities=(
                Fact(
                    value=modalities_from_capabilities(caps),
                    source="server_report",
                    observed_at=observed_at,
                    detail="ollama /api/show capabilities",
                )
                if capabilities_known
                else unknown()
            ),
            thinking=(
                Fact(
                    value=ThinkingSpec(mechanism="on_off" if "thinking" in caps else "none"),
                    source="server_report",
                    observed_at=observed_at,
                    detail="ollama /api/show capabilities",
                )
                if capabilities_known
                else unknown()
            ),
        )
        deployment = DeploymentCapabilities(
            provider_id=ctx.provider_id,
            api_base=ctx.api_base,
            model_id=model_id,
            model_key=model_key_fact,
        )
        discovered = DiscoveredModel(
            id=model_id,
            raw={**dict(raw), "arch": arch, "capabilities": list(caps)},
        )
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)
