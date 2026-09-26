"""Ollama handshake — thin orchestration over :mod:`clio_agent.providers.capabilities.dialects.ollama`.

This class owns ONLY connectivity dispatch and the per-phase plumbing
(:class:`~clio_agent.providers.handshake.base.ProviderHandshake`'s contract);
every HTTP read of Ollama's native API (``/api/tags``, ``/api/show``,
``/api/ps``) and every mapping of its fields onto the capability records lives
in :mod:`clio_agent.providers.capabilities.dialects.ollama` -- the single
place that talks to an Ollama server (consolidation review of #1447: this
class used to read/parse ``/api/show`` itself, duplicating the dialect
module).

We inherit the keyless connectivity probe from :class:`OpenAICompatHandshake`
(Ollama needs no API key) and only override discovery + per-model config to
route through the dialect adapter's native-API reads instead of the generic
OpenAI-compat ``/v1`` shim, which reports nothing useful for Ollama.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.api_base import native_root
from clio_agent.providers.capabilities.dialects import ollama as ollama_dialect
from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import DiscoveredModel, DiscoveredModelFacts
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake


class OllamaHandshake(OpenAICompatHandshake):
    """Ollama: list via ``/api/tags``, enrich each model via ``/api/show`` + ``/api/ps``."""

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List installed models from the native ``/api/tags``."""
        rows = await ollama_dialect.fetch_tags(client, native_root(ctx.api_base))
        return [r for r in rows if not self._is_embedding(r)]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Resolve one model's facts via ``/api/show`` + ``/api/ps`` (brief Part 6)."""
        model_id = str(raw.get("id") or raw.get("model") or "").strip()
        root = native_root(ctx.api_base)

        show_data = await ollama_dialect.fetch_show(client, root, model_id)
        ps_data = await ollama_dialect.fetch_ps(client, root)
        show_parameters = show_data.get("parameters") if isinstance(show_data, dict) else None

        observed_at = datetime.now(timezone.utc).isoformat()
        model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
        model = ollama_dialect.parse_show(
            show_data, model_key=model_key_fact.value or model_id, observed_at=observed_at
        )
        deployment = ollama_dialect.build_deployment_extra(
            provider_id=ctx.provider_id,
            api_base=ctx.api_base,
            model_id=model_id,
            show_parameters=show_parameters,
            ps_payload=ps_data,
        )

        arch, caps = ollama_dialect.show_identity(show_data)
        discovered = DiscoveredModel(
            id=model_id,
            raw={**dict(raw), "arch": arch, "capabilities": list(caps)},
        )
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)
