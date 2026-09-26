"""``OpenAICompatHandshake`` — the handshake for OpenAI-shaped HTTP backends.

Covers every provider that speaks the OpenAI ``/v1`` REST contract: cloud
OpenAI/Anthropic/Azure/Bedrock/Vertex/Gemini/NVIDIA NIM, OpenRouter, a
self-hosted vLLM server, and a single-mode llama.cpp server. The probe is a
single authenticated ``GET {api_base}/models`` — the same call that lists the
catalog — so connectivity, auth and model discovery share one round trip's
worth of plumbing.

:meth:`discover_model_config` reads NOTHING itself beyond the bare ``id`` on a
``/models`` row: it resolves this endpoint's dialect
(:func:`clio_agent.providers.capabilities.endpoint.dialect_for_provider`, the
same resolution :class:`~clio_agent.providers.handshake.base.ProviderHandshake`
uses for the endpoint record) and calls that dialect's OWN adapter in
:mod:`clio_agent.providers.capabilities.dialects` for every field a server
self-reports -- vLLM's ``max_model_len``/``root``, OpenRouter's
``context_length``/``top_provider.*``/``supported_parameters``, llama.cpp's
``/props``, or a cloud dialect's "no restriction" deployment defaults. A
dialect with no adapter here (an unrecognized OpenAI-compatible server) gets a
bare model/deployment record; the base class's
:meth:`~clio_agent.providers.handshake.base.ProviderHandshake.enrich_capabilities`
step then resolves ``context_max`` through the community-catalog cascade
(models.dev / litellm / the local DB).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers import host_credentials
from clio_agent.providers.capabilities import endpoint as capability_endpoint
from clio_agent.providers.capabilities.dialects import cloud as cloud_dialect
from clio_agent.providers.capabilities.dialects import llama_cpp as llama_cpp_dialect
from clio_agent.providers.capabilities.dialects import openrouter as openrouter_dialect
from clio_agent.providers.capabilities.dialects import vllm as vllm_dialect
from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
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

#: ``provider_kind`` values that authenticate via Anthropic's header scheme
#: (``x-api-key`` + a pinned API version) rather than a bearer token.
_ANTHROPIC_KINDS = frozenset({"anthropic"})

#: The Anthropic API version pinned on every request (their ``/v1`` requires it).
_ANTHROPIC_VERSION = "2023-06-01"

#: ``provider_kind`` values that require no API key (purely local backends).
_NO_AUTH_KINDS = frozenset({"ollama", "vllm", "local"})

#: ``error_code`` of a key the provider refused -- the client words it as
#: "Your <provider> API key was rejected."
API_KEY_REJECTED = "api_key_rejected"

#: Substrings that mark a model row as an embedding/reranker model we skip — the
#: handshake catalogs only chat-completion models.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OpenAICompatHandshake(ProviderHandshake):
    """Handshake for OpenAI-compatible HTTP providers.

    One authenticated ``GET /models`` drives connectivity, auth and discovery.
    Per-model config is intentionally thin because these endpoints do not
    report a model's own ceiling; the base enrich step fills it in.
    """

    def _requires_key(self, ctx: HandshakeContext) -> bool:
        """Whether this provider needs an API key to authenticate.

        Local backends (Ollama, a bare vLLM server) accept any/no key; cloud
        providers (OpenAI, Anthropic, OpenRouter) require one.
        """
        if getattr(self.provider, "host_credentials", ""):
            return False  # signs with the host's credentials, never an API key
        return ctx.provider_kind not in _NO_AUTH_KINDS

    def _auth_header(self, ctx: HandshakeContext) -> dict[str, str]:
        """Build the auth + protocol headers for a request.

        Anthropic uses ``x-api-key`` plus the pinned ``anthropic-version`` header;
        everyone else uses a standard ``Authorization: Bearer`` token. Returns an
        empty (or version-only) header set when no key is present.
        """
        headers: dict[str, str] = {}
        if ctx.provider_kind in _ANTHROPIC_KINDS:
            headers["anthropic-version"] = _ANTHROPIC_VERSION
            if ctx.api_key:
                headers["x-api-key"] = ctx.api_key
        elif ctx.api_key:
            headers["Authorization"] = f"Bearer {ctx.api_key}"
        return headers

    def _key_check_url(self, ctx: HandshakeContext) -> str | None:
        """The provider's own key-check endpoint, when its model listing is public.

        Read from the provider registry (``Provider.key_check_path``), never a
        per-provider branch here.
        """
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        provider = get_provider(ctx.provider_id)
        path = provider.key_check_path if provider is not None else None
        return f"{ctx.api_base.rstrip('/')}{path}" if path else None

    def _rejected(self, status: int, headers: dict[str, str]) -> ConnectivityResult:
        return ConnectivityResult(
            connectivity=ConnectivityState.OK,
            auth=AuthState.REJECTED,
            error=f"{API_KEY_REJECTED}: the provider refused the API key (HTTP {status})",
            error_code=API_KEY_REJECTED,
            auth_header=headers,
        )

    async def _check_key(
        self, client: Any, ctx: HandshakeContext, headers: dict[str, str]
    ) -> ConnectivityResult | None:
        """Prove the key against ``key_check_path``; ``None`` when it is accepted.

        A 401/403 is a rejected key. Any other failure means the key could not
        be proven either way: reported as ``DEFERRED`` with the typed reason,
        never as a silent pass.
        """
        url = self._key_check_url(ctx)
        if url is None:
            return None
        try:
            response = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed DEFERRED reason
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=AuthState.DEFERRED,
                error=f"key_check_unavailable: {type(exc).__name__}: {exc}",
                auth_header=headers,
            )
        if response.status_code in (401, 403):
            return self._rejected(response.status_code, headers)
        if response.status_code >= 400:
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=AuthState.DEFERRED,
                error=f"key_check_unavailable: HTTP {response.status_code}",
                auth_header=headers,
            )
        return None

    def _models_url(self, ctx: HandshakeContext) -> str:
        """The ``/models`` listing URL for this provider's ``api_base``.

        OpenRouter's listing hides every non-text-output model unless asked for
        all output modalities (:func:`~clio_agent.providers.capabilities.
        dialects.openrouter.models_url`).
        """
        if self._dialect(ctx) == openrouter_dialect.DIALECT:
            return openrouter_dialect.models_url(ctx.api_base)
        return f"{ctx.api_base.rstrip('/')}/models"

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Probe ``GET {api_base}/models`` with the provider's auth header.

        - missing-but-required key -> ``(SKIPPED, MISSING)`` (don't waste a call);
        - HTTP 401/403 -> ``(OK, REJECTED)`` (reachable, credential refused);
        - any transport error -> ``(UNREACHABLE, ...)``;
        - otherwise -> ``(OK, OK)`` (or ``NOT_REQUIRED`` for keyless local kinds),
          carrying the resolved ``auth_header`` forward for later phases.
        """
        chain = getattr(self.provider, "host_credentials", "")
        if chain and not host_credentials.present(chain):
            return ConnectivityResult(
                connectivity=ConnectivityState.SKIPPED,
                auth=AuthState.MISSING,
                error=host_credentials.missing_reason(chain),
                error_code=host_credentials.HOST_CREDENTIALS_MISSING,
            )
        if self._requires_key(ctx) and not ctx.api_key:
            return ConnectivityResult(
                connectivity=ConnectivityState.SKIPPED,
                auth=AuthState.MISSING,
                error="no API key provided",
            )
        headers = self._auth_header(ctx)
        try:
            response = await client.get(self._models_url(ctx), headers=headers)
        except Exception as exc:  # transport-level failure -> unreachable  # noqa: BLE001 - surfaced as UNREACHABLE connectivity
            return ConnectivityResult(
                connectivity=ConnectivityState.UNREACHABLE,
                auth=AuthState.MISSING if self._requires_key(ctx) else AuthState.NOT_REQUIRED,
                error=f"{type(exc).__name__}: {exc}",
            )
        status = response.status_code
        if status in (401, 403):
            return self._rejected(status, headers)
        if self._requires_key(ctx) and status < 400:
            key_check = await self._check_key(client, ctx, headers)
            if key_check is not None:
                return key_check
        auth_ok = AuthState.OK if self._requires_key(ctx) else AuthState.NOT_REQUIRED
        if status >= 400:
            # Reachable but the listing failed for a non-auth reason (e.g. 404 on
            # Ollama's /v1/models, which then falls back to /api/tags). The
            # credential itself was not rejected, so report it as accepted/not
            # required and let discovery surface (or recover) the model list.
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=auth_ok,
                error=f"models listing returned HTTP {status}",
                auth_header=headers,
            )
        auth = auth_ok
        return ConnectivityResult(
            connectivity=ConnectivityState.OK,
            auth=auth,
            auth_header=headers,
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List the provider's models as raw rows.

        Parses the OpenAI ``{"data": [{"id", ...}]}`` shape (or a bare list, for
        a server that skips the wrapper). Ollama routes through :class:`~clio_agent.providers.handshake.ollama.
        OllamaHandshake` instead (its native ``/api/tags`` reports nothing
        useful through this generic ``/models`` shim). Every row is listed: a
        surrogate (embedding, rerank, ...) is a first-class model identified by its
        task fact and refused only as the CHAT model, never dropped by its name.
        """
        headers = self._auth_header(ctx)
        rows: list[dict[str, Any]] = []
        try:
            response = await client.get(self._models_url(ctx), headers=headers)
            if response.status_code < 400:
                payload = response.json()
                rows = self._rows_from_openai_payload(payload)
        except Exception:  # noqa: BLE001 - unparseable models payload yields no rows
            rows = []
        return rows

    def _rows_from_openai_payload(self, payload: Any) -> list[dict[str, Any]]:
        """Extract model rows from an OpenAI ``/models`` JSON payload."""
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        if isinstance(payload, list):  # some servers return a bare list
            return [r for r in payload if isinstance(r, dict)]
        return []

    def _dialect(self, ctx: HandshakeContext) -> str:
        """Resolve this endpoint's dialect the SAME way the endpoint record does.

        (:func:`clio_agent.providers.handshake.base.ProviderHandshake.
        _record_endpoint_capabilities` computes it identically -- both read the
        registry ``Provider`` row's own ``litellm_prefix`` rather than
        ``ctx.provider_kind`` alone, since kind only selects the wire format
        and collapses several real server types onto the same value.)
        """
        litellm_prefix = str(getattr(self.provider, "litellm_prefix", "") or ctx.provider_kind)
        return capability_endpoint.dialect_for_provider(
            ctx.provider_kind, litellm_prefix, ctx.provider_id
        )

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Build a :class:`DiscoveredModelFacts` from one ``/models`` row.

        Dispatches to this endpoint's OWN dialect adapter
        (:mod:`clio_agent.providers.capabilities.dialects`) for every field a
        server self-reports on the row -- this class reads and parses nothing
        dialect-specific itself. A dialect with no adapter here gets a bare
        model/deployment record; the base ``enrich_capabilities`` step then
        resolves ``context_max`` via the community-catalog cascade.
        """
        model_id = str(raw.get("id", "")).strip()
        dialect = self._dialect(ctx)

        if dialect == vllm_dialect.DIALECT:
            deployment = vllm_dialect.parse_models_row(
                raw, provider_id=ctx.provider_id, api_base=ctx.api_base
            )
            model_key = deployment.model_key.value or model_id
            model = vllm_dialect.build_model_capabilities(model_key, raw)
            model = await self._compare_against_native_context(model, deployment, model_id)
        elif dialect == openrouter_dialect.DIALECT:
            model, deployment = openrouter_dialect.parse_model_row(
                raw, provider_id=ctx.provider_id, api_base=ctx.api_base
            )
        elif dialect == llama_cpp_dialect.DIALECT:
            from clio_agent.providers.api_base import native_root  # noqa: PLC0415

            props = await llama_cpp_dialect.fetch_props(client, native_root(ctx.api_base))
            deployment = llama_cpp_dialect.parse_props(
                props or {}, provider_id=ctx.provider_id, api_base=ctx.api_base, model_id=model_id
            )
            model_key = deployment.model_key.value or model_id
            model = llama_cpp_dialect.build_model_capabilities(model_key, {"data": [raw]}, model_id)
        elif dialect in cloud_dialect.CLOUD_DIALECTS:
            deployment = cloud_dialect.build_deployment_capabilities(
                ctx.provider_id, ctx.api_base, model_id
            )
            model = ModelCapabilities(model_key=self._bare_model_key(model_id))
        else:
            # No adapter for this dialect (an unrecognized OpenAI-compatible
            # server): a bare record, no field guessing. The base class's
            # enrich_capabilities cascade is the only thing that can fill this in.
            model = ModelCapabilities(model_key=self._bare_model_key(model_id))
            deployment = DeploymentCapabilities(
                provider_id=ctx.provider_id,
                api_base=ctx.api_base,
                model_id=model_id,
                model_key=deployment_model_key_fact(model_id, observed_at=_now_iso()),
            )

        discovered = DiscoveredModel(id=model_id, raw=dict(raw))
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)

    def _bare_model_key(self, model_id: str) -> str:
        fact = deployment_model_key_fact(model_id, observed_at=_now_iso())
        return fact.value or model_id

    async def _compare_against_native_context(
        self, model: ModelCapabilities, deployment: DeploymentCapabilities, model_id: str
    ) -> ModelCapabilities:
        """vLLM-only: an OFFLINE-ONLY (no network) catalog lookup for the model's own
        published maximum, so the deployment's self-reported served window can be
        compared against it (Part 3's context_window_below_native warning) even
        when ``allow_external_sources=False`` keeps the network-allowed cascade
        in ``enrich_capabilities`` from running. vLLM's own ``/v1/models`` row
        carries no model-level ceiling (:func:`vllm_dialect.build_model_capabilities`
        always returns one unknown), so this only ever fills a gap, never
        overwrites a dialect's own self-reported ceiling.
        """
        if model.context_max.known or not deployment.context_served.known or not model_id:
            return model
        from dataclasses import replace  # noqa: PLC0415

        from clio_agent.providers.handshake.sources import lookup_native_context  # noqa: PLC0415

        native_context_max = lookup_native_context(model_id)
        if native_context_max is None:
            return model
        return replace(
            model,
            context_max=Fact(
                value=native_context_max,
                source="litellm",
                observed_at=_now_iso(),
                detail="offline catalog lookup (no network), compared against the served window",
            ),
        )
