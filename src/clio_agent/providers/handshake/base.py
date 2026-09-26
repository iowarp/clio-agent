"""``ProviderHandshake`` — the async phase template every provider handshake runs.

The base class owns the cross-cutting concerns (an ``httpx.AsyncClient`` with
per-phase timeouts, wall-clock latency, and turning *any* exception into a typed
:class:`HandshakeReport` rather than raising). Subclasses implement only the
provider-specific phases:

    connectivity + auth  ->  discover models  ->  per-model config  ->  enrich capabilities

Per-model config now returns a :class:`~clio_agent.providers.handshake.model.
DiscoveredModelFacts` — bare identity plus the
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities` /
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities`
facts the adapter evidenced — instead of the deleted flat ``ModelProfile``.
This class writes both records into the shared store
(:mod:`clio_agent.providers.capabilities.invalidation`) as each model is
discovered, and once per run builds and stores the endpoint's own
:class:`~clio_agent.providers.capabilities.records.EndpointCapabilities` from
the registry ``Provider``'s dialect (:mod:`clio_agent.providers.capabilities.
endpoint`). :class:`HandshakeReport` itself keeps only the bare
:class:`~clio_agent.providers.handshake.model.DiscoveredModel` identity list —
every capability question routes through
:func:`clio_agent.providers.capabilities.accessor.get_effective_capabilities`.

The enrich step resolves a missing model ``context_max``/``output_max``
through the community-catalog tier
(:mod:`clio_agent.providers.capabilities.model_sources`) — see
:meth:`ProviderHandshake.enrich_capabilities`.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    DiscoveredModelFacts,
    HandshakeReport,
)

logger = logging.getLogger(__name__)

#: Endpoint dialects that serve Hugging Face weights under their repo id (or a
#: link rule resolves one), so an ``org/name`` model key there is worth asking
#: the Hub about. Cloud APIs and aggregators name their own catalogs (an
#: OpenRouter ``openai/gpt-4o`` slug is not a repo), so they never reach the
#: Hugging Face layer.
_HF_REPO_DIALECTS: frozenset[str] = frozenset({"vllm", "llama_cpp", "lm_studio", "ollama"})

#: How many model rows are discovered + enriched at once. Enrichment reads the
#: community catalogs and the Hugging Face layer (network on a cold cache), so
#: a 40-model gateway must not run them one after another.
_MODEL_ROW_CONCURRENCY = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def describe_exception(exc: BaseException) -> str:
    """Return a non-empty, actionable description of ``exc``.

    Some exceptions (a bare ``ImportError()``, certain C-extension errors)
    carry an empty ``str()``. Left alone, every ``f"... failed: {exc}"`` site
    below turned that into a reason with no cause attached -- a real failure
    (e.g. a missing optional dependency) reported to the catalog as an EMPTY
    string, which is how a broken provider was able to read as healthy. Falls
    back to the type name + ``repr()`` so the real cause always reaches the
    trace/API (the no-silent-fallback ground rule) instead of vanishing.
    """
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: {exc!r}"


@dataclass
class ConnectivityResult:
    """Outcome of the connectivity + auth phase."""

    connectivity: ConnectivityState
    auth: AuthState
    error: str | None = None
    # A typed code for ``error`` when the failure is a KNOWN condition a
    # caller needs to branch on (e.g. a missing optional dependency) --
    # threaded onto the resulting HandshakeReport.error_code. Empty for an
    # ordinary failure with no such code.
    error_code: str = ""
    # Auth material resolved during the probe, reused by later phases (e.g. a
    # bearer token) so we authenticate once.
    auth_header: dict[str, str] = field(default_factory=dict)


class DiscoveryAuthRejected(Exception):
    """A provider rejected the credential only once model listing was attempted.

    ``check_connectivity`` for some providers (Argonne/ALCF) resolves a token
    without a network call, so a token that is syntactically present but
    rejected by the provider's own policy (e.g. ALCF's "high-assurance
    timeout") is invisible until ``discover_models`` actually calls the
    ``/models`` endpoint. Raising this from ``discover_models`` downgrades the
    resulting :class:`~clio_agent.providers.handshake.model.HandshakeReport`'s
    ``auth`` to :data:`AuthState.REJECTED` (instead of leaving the connectivity
    phase's stale ``OK``), so ``report.ok`` is False and health stops reporting
    ``ready`` — the typed ``reason``/``detail`` become the report's ``error``
    instead of a bare HTTP status string.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass
class HandshakeContext:
    """Inputs to a handshake.

    ``auth_mode`` gates credential acquisition: ``passive`` (health/doctor) must
    never trigger an interactive flow or a network call when no credential is
    present; ``active`` (explicit bind) may refresh a stored token but still must
    not pop a browser. ``allow_external_sources`` enables the models.dev /
    marketplace fallback for context windows; ``mutate_runtime`` permits a side
    effect like an LM Studio reload (set only on explicit bind).
    """

    provider_id: str
    provider_kind: str
    api_base: str
    api_key: str = ""
    target_model: str = ""
    auth_mode: str = "passive"  # "passive" | "active"
    allow_external_sources: bool = True
    mutate_runtime: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderHandshake(abc.ABC):
    """Abstract per-provider handshake. Subclass and implement the phase methods."""

    #: per-phase HTTP timeouts (seconds); subclasses may override.
    timeout_connect: float = 4.0
    timeout_models: float = 8.0
    timeout_model_config: float = 8.0

    def __init__(self, provider: Any) -> None:
        #: the registry ``Provider`` row this handshake serves.
        self.provider = provider

    async def handshake(self, ctx: HandshakeContext) -> HandshakeReport:
        """Run the full phase sequence, never raising.

        Returns a :class:`HandshakeReport`; connectivity/auth failures short-circuit
        with ``models=()`` and an actionable ``error``.
        """
        started = time.monotonic()
        try:
            client = await self._open_client(ctx)
        except Exception as exc:  # client construction should not fail, but be safe  # noqa: BLE001 - surfaced in HandshakeReport.error
            return self._report(
                ctx,
                ConnectivityState.UNREACHABLE,
                AuthState.MISSING,
                error=f"client init failed: {describe_exception(exc)}",
                started=started,
            )
        try:
            conn = await self.check_connectivity(client, ctx)
            if conn.connectivity != ConnectivityState.OK:
                return self._report(
                    ctx,
                    conn.connectivity,
                    conn.auth,
                    error=conn.error,
                    error_code=conn.error_code,
                    started=started,
                )
            if conn.auth == AuthState.REJECTED:
                # A refused credential ends the handshake: listing (and
                # enriching) a public catalog under it proves nothing and, for
                # a provider with hundreds of models, costs seconds.
                return self._report(
                    ctx,
                    ConnectivityState.OK,
                    AuthState.REJECTED,
                    error=conn.error,
                    error_code=conn.error_code,
                    started=started,
                )
            # thread the auth material resolved during connectivity to later phases
            if conn.auth_header:
                ctx.extra["auth_header"] = conn.auth_header
            try:
                raw_models = await self.discover_models(client, ctx)
            except DiscoveryAuthRejected as exc:
                return self._report(
                    ctx,
                    ConnectivityState.OK,
                    AuthState.REJECTED,
                    error=str(exc),
                    started=started,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in HandshakeReport.error
                return self._report(
                    ctx,
                    ConnectivityState.OK,
                    conn.auth,
                    error=f"model discovery failed: {describe_exception(exc)}",
                    started=started,
                )
            await self._record_endpoint_capabilities(client, ctx)
            gate = asyncio.Semaphore(_MODEL_ROW_CONCURRENCY)

            async def _one_row(raw: Any) -> DiscoveredModelFacts | None:
                async with gate:
                    try:
                        facts = await self.discover_model_config(client, ctx, raw)
                        return await self.enrich_capabilities(facts, ctx)
                    except Exception as exc:  # noqa: BLE001 - one bad row is dropped with a typed reason
                        # One bad model row must not sink the whole report, but a
                        # dropped row is not silent: emit a structured reason so it
                        # reaches the logs rather than vanishing.
                        _model = raw.get("id") if isinstance(raw, dict) else getattr(raw, "id", raw)
                        logger.warning(
                            "handshake dropped a model row: reason=model_row_discovery_failed model=%r error=%r",
                            _model,
                            exc,
                        )
                        return None

            # Rows run concurrently but are recorded in the server's own order.
            discovered: list[DiscoveredModel] = []
            for facts in await asyncio.gather(*(_one_row(raw) for raw in raw_models)):
                if facts is None:
                    continue
                self._record_model_facts(facts)
                discovered.append(facts.discovered)
            # An unproven credential keeps its typed reason even when the
            # model listing itself answered (a public listing).
            auth_reason = conn.auth == AuthState.DEFERRED
            return self._report(
                ctx,
                ConnectivityState.OK,
                conn.auth,
                models=tuple(discovered),
                error=conn.error if auth_reason else None,
                error_code=conn.error_code if auth_reason else "",
                started=started,
            )
        except Exception as exc:  # final backstop — never raise out of a handshake  # noqa: BLE001 - final backstop surfaced in HandshakeReport.error
            return self._report(
                ctx,
                ConnectivityState.UNREACHABLE,
                AuthState.MISSING,
                error=f"handshake error: {describe_exception(exc)}",
                started=started,
            )
        finally:
            await self._close_client(client)

    # ------------------------------------------------------------------ phases
    @abc.abstractmethod
    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """One cheap authenticated probe; the gate for everything downstream."""

    @abc.abstractmethod
    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List the provider's models as raw provider-shaped rows."""

    @abc.abstractmethod
    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Build a :class:`DiscoveredModelFacts` from one raw row's self-reported fields."""

    async def enrich_capabilities(
        self, facts: DiscoveredModelFacts, ctx: HandshakeContext
    ) -> DiscoveredModelFacts:
        """Resolve the model record through the brief 5.1 precedence below ``server_report``.

        The adapter's own ``server_report`` facts win field by field; what they
        leave unknown comes from the Hugging Face repo layer (brief 6.1:
        ``config.json`` / processor / ``params.json`` modalities, the
        ``pipeline_tag`` model type, sampling, a chat-template scan) and then the
        community catalogs (models.dev -> litellm -> the local DB), via
        :func:`~clio_agent.providers.capabilities.model_sources.resolve_model_capabilities`.
        The Hugging Face layer is consulted only for an ``org/name`` key on a
        dialect that serves Hub weights (:data:`_HF_REPO_DIALECTS`). Runs off the
        event loop: both layers read disk caches and, when cold, the network.
        Gated on ``allow_external_sources``. A field no layer states stays
        UNKNOWN -- a server that reports no modalities is never read as text-only.
        """
        if not ctx.allow_external_sources:
            return facts
        from clio_agent.providers.capabilities.model_sources import (  # noqa: PLC0415
            resolve_model_capabilities,
        )

        model = facts.model
        hf_source = self._hf_source(ctx, model.model_key)
        resolved = await asyncio.to_thread(
            resolve_model_capabilities,
            model.model_key,
            server_report=model,
            hf_repo=hf_source,
            community_lookup_id=facts.discovered.id,
        )
        if resolved == model:
            return facts
        return replace(facts, model=resolved)

    def _hf_source(self, ctx: HandshakeContext, model_key: str) -> Any:
        """The Hugging Face layer for ``model_key`` on this endpoint, or ``None``."""
        from clio_agent.providers.capabilities import invalidation  # noqa: PLC0415
        from clio_agent.providers.capabilities.hf_repo import (  # noqa: PLC0415
            HfRepoCatalogSource,
            is_repo_id,
        )
        from clio_agent.providers.identity import endpoint_key  # noqa: PLC0415

        if not is_repo_id(model_key):
            return None
        endpoint = invalidation.get_endpoint_capabilities(
            endpoint_key(ctx.provider_id, ctx.api_base)
        )
        if endpoint is None or endpoint.dialect not in _HF_REPO_DIALECTS:
            return None
        return HfRepoCatalogSource()

    # ------------------------------------------------------------------ helpers
    def _record_model_facts(self, facts: DiscoveredModelFacts) -> None:
        """Write one discovered model's facts into the shared capability store."""
        from clio_agent.providers.capabilities import invalidation  # noqa: PLC0415

        invalidation.record_model_capabilities(facts.model)
        invalidation.record_deployment_capabilities(facts.deployment)

    async def _record_endpoint_capabilities(self, client: Any, ctx: HandshakeContext) -> None:
        """Build and store this endpoint's :class:`EndpointCapabilities` once per run.

        Uses the registry ``Provider`` row's own ``litellm_prefix`` (LiteLLM's
        ``custom_llm_provider`` name) rather than ``ctx.provider_kind`` alone,
        since ``provider_kind`` only selects the wire FORMAT (Part 3) and
        collapses several real server types (llama.cpp, a bare vLLM server,
        cloud OpenAI-compatible) onto the same kind.

        The dialect's own invalidation fingerprint (brief 5.6) is resolved via
        :meth:`_dialect_endpoint_fingerprint` -- one best-effort extra GET for a
        dialect this base class knows how to fingerprint (llama.cpp ``/props``
        ``build_info``, vLLM ``/version``, Ollama ``/api/version``); a failure
        there never blocks recording the rest of this endpoint's facts.
        """
        from clio_agent.providers.capabilities import (
            endpoint as capability_endpoint,  # noqa: PLC0415
        )
        from clio_agent.providers.capabilities import invalidation  # noqa: PLC0415

        litellm_prefix = str(getattr(self.provider, "litellm_prefix", "") or ctx.provider_kind)
        dialect = capability_endpoint.dialect_for_provider(
            ctx.provider_kind, litellm_prefix, ctx.provider_id
        )
        server_version, fingerprint = await self._dialect_endpoint_fingerprint(client, ctx, dialect)
        caps = capability_endpoint.build_endpoint_capabilities(
            ctx.provider_id,
            ctx.api_base,
            dialect,
            ctx.target_model or "",
            custom_llm_provider=litellm_prefix,
            server_version=server_version,
            fingerprint=fingerprint,
        )
        invalidation.record_endpoint_capabilities(caps)

    async def _dialect_endpoint_fingerprint(
        self, client: Any, ctx: HandshakeContext, dialect: str
    ) -> tuple[Any, str]:
        """Best-effort per-dialect endpoint fingerprint (brief 5.6).

        Returns ``(server_version_fact_or_none, fingerprint)``. Delegates the
        HTTP read itself to each dialect's OWN ``fetch_*`` function (this
        method reads/parses NOTHING dialect-specific) and reuses its pure
        ``fingerprint_from_*`` for the fingerprint shape -- both live in
        exactly one place, :mod:`clio_agent.providers.capabilities.dialects`.
        Any failure (older server, transient error, a dialect this base class
        has no fingerprint for) degrades to ``(None, "")`` -- an endpoint with
        no fingerprint yet is exactly today's (pre-P4b) behavior, never a hard
        failure.
        """
        from clio_agent.providers.api_base import native_root  # noqa: PLC0415
        from clio_agent.providers.capabilities.dialects import (  # noqa: PLC0415
            llama_cpp as llama_cpp_dialect,
        )
        from clio_agent.providers.capabilities.dialects import (
            ollama as ollama_dialect,  # noqa: PLC0415
        )
        from clio_agent.providers.capabilities.dialects import vllm as vllm_dialect  # noqa: PLC0415
        from clio_agent.providers.capabilities.records import Fact  # noqa: PLC0415

        def _fact(text: str, detail: str) -> Fact | None:
            return Fact(text, "server_report", _now_iso(), detail) if text else None

        try:
            if dialect == llama_cpp_dialect.DIALECT:
                props = await llama_cpp_dialect.fetch_props(client, native_root(ctx.api_base))
                build_info = (props or {}).get("build_info")
                return (
                    _fact(str(build_info or "").strip(), "llama.cpp /props build_info"),
                    llama_cpp_dialect.fingerprint_from_build_info(build_info),
                )
            if dialect == vllm_dialect.DIALECT:
                version = await vllm_dialect.fetch_version(client, ctx.api_base)
                return (
                    _fact(str(version or "").strip(), "vllm /version"),
                    vllm_dialect.fingerprint_from_version(version),
                )
            if dialect == ollama_dialect.DIALECT:
                version = await ollama_dialect.fetch_version(client, native_root(ctx.api_base))
                return (
                    _fact(str(version or "").strip(), "ollama /api/version"),
                    ollama_dialect.fingerprint_from_version(version),
                )
        except Exception as exc:  # noqa: BLE001 - fingerprinting is best-effort, never sinks discovery
            logger.debug(
                "handshake: dialect endpoint fingerprint failed dialect=%s: %s", dialect, exc
            )
        return None, ""

    def models_provenance(self, ctx: HandshakeContext) -> tuple[str, str]:
        """Return ``(models_source, evidence_generated_at)`` for a completed run.

        The base answer is ``("live", "")`` because every HTTP handshake really
        does probe the provider's ``/models`` surface on the run that produced
        the profiles. Subclasses that do NOT probe must say so: the zero-network
        CLI handshakes override this, so a catalog read can no longer present
        itself as a live probe. ``evidence_generated_at`` is empty when the
        evidence IS this run (the caller then uses ``generated_at``).
        """

        del ctx
        return "live", ""

    async def _open_client(self, ctx: HandshakeContext) -> Any:
        import httpx  # noqa: PLC0415

        timeout = httpx.Timeout(
            connect=self.timeout_connect,
            read=max(self.timeout_models, self.timeout_model_config),
            write=self.timeout_connect,
            pool=self.timeout_connect,
        )
        return httpx.AsyncClient(timeout=timeout)

    async def _close_client(self, client: Any) -> None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001,S110 - client close best-effort during teardown
            pass

    def _report(
        self,
        ctx: HandshakeContext,
        connectivity: ConnectivityState,
        auth: AuthState,
        *,
        models: tuple[DiscoveredModel, ...] = (),
        error: str | None = None,
        error_code: str = "",
        started: float | None = None,
    ) -> HandshakeReport:
        latency = None if started is None else (time.monotonic() - started) * 1000.0
        now = datetime.now(timezone.utc).isoformat()
        if error and not models:
            source, evidence_generated_at = "unavailable", ""
        else:
            source, evidence_generated_at = self.models_provenance(ctx)
        return HandshakeReport(
            provider_id=ctx.provider_id,
            provider_kind=ctx.provider_kind,
            connectivity=connectivity,
            auth=auth,
            api_base=ctx.api_base,
            latency_ms=latency,
            error=error,
            error_code=error_code,
            models=models,
            models_source=source,
            generated_at=now,
            evidence_generated_at=evidence_generated_at or now,
        )
