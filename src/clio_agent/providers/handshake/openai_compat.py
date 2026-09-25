"""``OpenAICompatHandshake`` — the handshake for OpenAI-shaped HTTP backends.

Covers every provider that speaks the OpenAI ``/v1`` REST contract: cloud OpenAI
and Anthropic, OpenRouter, a self-hosted vLLM server, and Ollama (which also
exposes a compatible ``/v1/models``, with a ``/api/tags`` fallback). The probe is
a single authenticated ``GET {api_base}/models`` — the same call that lists the
catalog — so connectivity, auth and model discovery share one round trip's worth
of plumbing.

None of these endpoints report a model's own MAXIMUM context through
``/models`` (OpenAI returns only ``{"id", "object", ...}``), so
:meth:`OpenAICompatHandshake.discover_model_config` always leaves
``ModelCapabilities.context_max`` unknown; the base class's
:meth:`~clio_agent.providers.handshake.base.ProviderHandshake.enrich_capabilities`
step then resolves it through the community-catalog cascade (models.dev /
litellm / the local DB). Some backends DO self-report what they are CURRENTLY
SERVING on the ``/models`` row itself (vLLM's ``max_model_len``, OpenRouter's
``context_length`` / ``top_provider.*``) — per the brief (Part 6) that is a
**deployment** fact, not the model's own ceiling, so it is recorded as
``DeploymentCapabilities.context_served``/``output_max`` instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
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

#: ``provider_kind`` values that authenticate via Anthropic's header scheme
#: (``x-api-key`` + a pinned API version) rather than a bearer token.
_ANTHROPIC_KINDS = frozenset({"anthropic"})

#: The Anthropic API version pinned on every request (their ``/v1`` requires it).
_ANTHROPIC_VERSION = "2023-06-01"

#: ``provider_kind`` values that require no API key (purely local backends).
_NO_AUTH_KINDS = frozenset({"ollama", "vllm", "local"})

#: Substrings that mark a model row as an embedding/reranker model we skip — the
#: handshake catalogs only chat-completion models.
_EMBEDDING_MARKERS = ("embed", "embedding", "rerank", "reranker")


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

    def _models_url(self, ctx: HandshakeContext) -> str:
        """The ``/models`` listing URL for this provider's ``api_base``."""
        return f"{ctx.api_base.rstrip('/')}/models"

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Probe ``GET {api_base}/models`` with the provider's auth header.

        - missing-but-required key -> ``(SKIPPED, MISSING)`` (don't waste a call);
        - HTTP 401/403 -> ``(OK, REJECTED)`` (reachable, credential refused);
        - any transport error -> ``(UNREACHABLE, ...)``;
        - otherwise -> ``(OK, OK)`` (or ``NOT_REQUIRED`` for keyless local kinds),
          carrying the resolved ``auth_header`` forward for later phases.
        """
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
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=AuthState.REJECTED,
                error=f"auth rejected (HTTP {status})",
                auth_header=headers,
            )
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
        """List the provider's chat models as raw rows.

        Parses the OpenAI ``{"data": [{"id", ...}]}`` shape. For Ollama, falls back
        to ``GET {root}/api/tags`` (``{"models": [{"model"|"name"}]}``) when the
        ``/v1/models`` route is unavailable. Embedding/reranker rows are dropped.
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
        if not rows and ctx.provider_kind == "ollama":
            rows = await self._discover_ollama_tags(client, ctx, headers)
        return [r for r in rows if not self._is_embedding(r)]

    def _rows_from_openai_payload(self, payload: Any) -> list[dict[str, Any]]:
        """Extract model rows from an OpenAI ``/models`` JSON payload."""
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        if isinstance(payload, list):  # some servers return a bare list
            return [r for r in payload if isinstance(r, dict)]
        return []

    async def _discover_ollama_tags(
        self, client: Any, ctx: HandshakeContext, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Ollama fallback: ``GET {root}/api/tags`` -> normalized ``{"id"}`` rows.

        The ``root`` is ``api_base`` with a trailing ``/v1`` stripped, since the
        native Ollama API lives at the server root, not under ``/v1``.
        """
        base = ctx.api_base.rstrip("/")
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        url = f"{root}/api/tags"
        try:
            response = await client.get(url, headers=headers)
            if response.status_code >= 400:
                return []
            payload = response.json()
        except Exception:  # noqa: BLE001 - unparseable payload yields no models
            return []
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return []
        rows: list[dict[str, Any]] = []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("model") or entry.get("name")
            if model_id:
                rows.append({"id": model_id, **entry})
        return rows

    def _is_embedding(self, raw: dict[str, Any]) -> bool:
        """Heuristically detect an embedding/reranker row to skip it."""
        model_id = str(raw.get("id", "")).lower()
        if any(marker in model_id for marker in _EMBEDDING_MARKERS):
            return True
        row_type = str(raw.get("type", "")).lower()
        return row_type in {"embeddings", "embedding", "rerank", "reranker"}

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Build a :class:`DiscoveredModelFacts` from one ``/models`` row.

        Bare OpenAI/Anthropic ``/models`` rows carry only an id, so the model
        record is thin and the base ``enrich_capabilities`` step resolves the
        ceiling via the community-catalog cascade. But some OpenAI-compatible
        backends DO self-report what they're CURRENTLY SERVING on the row,
        which is a **deployment** fact (brief Part 6):

        * **vLLM** -> ``max_model_len`` (the served context window);
        * **OpenRouter** -> ``context_length`` / ``top_provider.context_length``
          and ``top_provider.max_completion_tokens`` for the active route.
        """
        model_id = str(raw.get("id", "")).strip()
        observed_at = _now_iso()
        _tp = raw.get("top_provider")
        top: dict[str, Any] = _tp if isinstance(_tp, dict) else {}
        context_served = _first_positive_int(
            raw.get("max_model_len"),  # vLLM
            raw.get("context_length"),  # OpenRouter (top-level)
            top.get("context_length"),  # OpenRouter (active route)
        )
        output_served = _first_positive_int(
            raw.get("max_completion_tokens"),
            top.get("max_completion_tokens"),
        )
        # vLLM's /v1/models row names the Hugging Face repo it loaded as `root`
        # (brief 5.4 rule 2); every other OpenAI-shaped backend has nothing
        # comparable, so this is None for them and the wire id stands in.
        vllm_root = raw.get("root")
        model_key_fact = deployment_model_key_fact(
            model_id,
            observed_at=observed_at,
            vllm_root=str(vllm_root) if isinstance(vllm_root, str) and vllm_root else None,
        )
        model_key = model_key_fact.value or model_id
        # An OFFLINE-ONLY (no network) catalog lookup for the model's own published
        # maximum, so the deployment's self-reported served window can be compared
        # against it (Part 3's context_window_below_native warning) even when
        # ``allow_external_sources=False`` keeps the network-allowed cascade in
        # ``enrich_capabilities`` from running. Only attempted when a served window
        # is actually known -- otherwise there's nothing to compare it against.
        native_context_max: int | None = None
        if context_served is not None and model_id:
            from clio_agent.providers.handshake.sources import (
                lookup_native_context,  # noqa: PLC0415
            )

            native_context_max = lookup_native_context(model_id)
        model = ModelCapabilities(
            model_key=model_key,
            context_max=(
                Fact(
                    value=native_context_max,
                    source="litellm",
                    observed_at=observed_at,
                    detail="offline catalog lookup (no network), compared against the served window",
                )
                if native_context_max is not None
                else unknown()
            ),
        )
        deployment = DeploymentCapabilities(
            provider_id=ctx.provider_id,
            api_base=ctx.api_base,
            model_id=model_id,
            model_key=model_key_fact,
            context_served=(
                Fact(
                    value=context_served,
                    source="server_report",
                    observed_at=observed_at,
                    detail="self-reported on the /models row (max_model_len / context_length)",
                )
                if context_served is not None
                else unknown()
            ),
            output_max=(
                Fact(
                    value=output_served,
                    source="server_report",
                    observed_at=observed_at,
                    detail="self-reported on the /models row (max_completion_tokens)",
                )
                if output_served is not None
                else unknown()
            ),
        )
        discovered = DiscoveredModel(id=model_id, raw=dict(raw))
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)


def _first_positive_int(*values: Any) -> int | None:
    """Return the first value that is a positive int (ignoring bools/None/0)."""
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None
