"""LM Studio provider handshake.

LM Studio exposes an OpenAI-compatible API under ``{api_base}`` (e.g.
``http://host:1234/v1``) plus a richer, native ``/api/v0`` surface served from
the same host root (``http://host:1234``). The native ``/api/v0/models``
endpoint is what makes LM Studio worth a bespoke handshake: it self-reports the
fields clio needs to size requests correctly — ``max_context_length`` (the model
ceiling), ``loaded_context_length`` (the *runtime* window an already-loaded model
is actually serving), the ``quantization`` / ``arch`` of the GGUF, the load
``state``, and a ``capabilities`` list (``"tool_use"`` => native tool calling).

LM Studio is a local backend with no authentication, so the connectivity probe
reports :data:`AuthState.NOT_REQUIRED`. The probe hits the native endpoint first
and falls back to the OpenAI-compatible ``{api_base}/models`` so that a stripped
build (or an older LM Studio) still registers as reachable.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    ModelProfile,
)


class LMStudioHandshake(ProviderHandshake):
    """Handshake for a local LM Studio backend (no auth, native ``/api/v0``)."""

    #: ``/api/v0/models`` reports a per-model ``capabilities`` list (``vision``,
    #: ``tool_use``), so this backend really can evidence input modalities.
    reports_input_modalities = True

    @staticmethod
    def _root(api_base: str) -> str:
        """Return the host root for ``api_base`` by stripping a trailing ``/v1``.

        ``http://host:1234/v1`` -> ``http://host:1234``. Any trailing slashes are
        normalised away first so ``.../v1/`` is handled too.
        """
        base = api_base.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Probe LM Studio; native ``/api/v0/models`` first, OpenAI ``/models`` fallback.

        Either endpoint answering marks the backend reachable. LM Studio requires
        no credential, so auth is always :data:`AuthState.NOT_REQUIRED`.
        """
        root = self._root(ctx.api_base)
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
        root = self._root(ctx.api_base)
        response = await client.get(f"{root}/api/v0/models")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Build a :class:`ModelProfile` from one ``/api/v0`` model row.

        Maps LM Studio's self-reported fields:
        ``max_context_length`` -> ``context_window`` (the ceiling),
        ``loaded_context_length`` -> ``loaded_context_window`` (runtime window),
        ``quantization``/``arch`` pass through, the ``capabilities`` list becomes
        a tuple with ``native_tool_calling`` set when it advertises ``"tool_use"``,
        and ``state == "loaded"`` sets ``is_loaded``. The provider self-reports the
        window, so ``context_source`` stays ``"live"``.
        """
        capabilities = raw.get("capabilities") or []
        if not isinstance(capabilities, list):
            capabilities = []
        caps = tuple(str(cap) for cap in capabilities)
        return ModelProfile(
            id=str(raw.get("id", "")),
            context_window=raw.get("max_context_length"),
            loaded_context_window=raw.get("loaded_context_length"),
            quantization=raw.get("quantization"),
            arch=raw.get("arch"),
            capabilities=caps,
            native_tool_calling="tool_use" in caps,
            is_loaded=raw.get("state") == "loaded",
            context_source="live",
            raw=raw,
        )
