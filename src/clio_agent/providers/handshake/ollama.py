"""Ollama handshake — grounded on the Ollama REST API.

Ollama exposes two native endpoints we use (the OpenAI-compat ``/v1`` shim reports
none of this):

* ``GET /api/tags`` — installed models (``{"models": [{"model", "name", ...}]}``).
* ``POST /api/show`` ``{"model": id}`` — per-model metadata:
  - ``model_info["general.architecture"]`` (e.g. ``"qwen3"``) and, keyed by that
    arch, ``model_info["<arch>.context_length"]`` — the **real context window**;
  - ``capabilities`` — a list that includes ``"tools"`` (native function-calling)
    and ``"thinking"`` (reasoning) when the model supports them.

So Ollama models resolve **live** with no catalog fallback needed. We inherit the
keyless connectivity probe from :class:`OpenAICompatHandshake` (Ollama needs no
API key) and only override discovery + per-model config to hit the native API.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import ModelProfile
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake


class OllamaHandshake(OpenAICompatHandshake):
    """Ollama: list via ``/api/tags``, enrich each model via ``/api/show``."""

    @staticmethod
    def _native_root(api_base: str) -> str:
        """The native API root — ``api_base`` with a trailing ``/v1`` stripped."""
        base = (api_base or "").rstrip("/")
        return base[: -len("/v1")] if base.endswith("/v1") else base

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List installed models from the native ``/api/tags``."""
        headers = self._auth_header(ctx)
        rows = await self._discover_ollama_tags(client, ctx, headers)
        return [r for r in rows if not self._is_embedding(r)]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Resolve one model's context window + capabilities via ``/api/show``."""
        model_id = str(raw.get("id") or raw.get("model") or "").strip()
        root = self._native_root(ctx.api_base)

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
        except Exception:
            # /api/show is best-effort: a failure leaves context_window=None and the
            # base enrich step falls back to the cascade (models.dev/litellm/DB).
            pass

        return ModelProfile(
            id=model_id,
            context_window=context_window,
            is_reasoning="thinking" in caps,
            native_tool_calling="tools" in caps,
            arch=arch,
            capabilities=caps,
            context_source="live",
            raw=dict(raw),
        )
