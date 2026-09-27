"""``NoOpHandshake`` — the handshake for CLI-driven providers with no HTTP surface.

Some providers (the ``codex`` and ``claude_code`` LiteLLM bridges) drive a local
CLI rather than an HTTP endpoint, so there is nothing to *probe*: no ``/models``
route, no auth header, no network at all. This base makes *zero* network calls
and lists no models itself; its subclasses
(:mod:`clio_agent.providers.handshake.cli_catalog`) supply rows from their own
offline data sources, and the base :meth:`ProviderHandshake.enrich_capabilities`
step then fills each model's context window from the shared source cascade
(provider-self-reported -> models.dev -> litellm catalog -> local DB).

This is what makes a CLI provider's context discoverable on default config
(iowarp/clio-agent#740). Previously discovery returned ``[]`` so codex /
claude_code models reached the picker and the budgeter with NO context window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
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


class NoOpHandshake(ProviderHandshake):
    """A handshake that probes nothing — for CLI providers (codex, claude_code).

    No network is touched: connectivity is reported ``OK``/``NOT_REQUIRED`` (the
    CLI is local and manages its own auth), and ``discover_models`` lists nothing;
    subclasses supply rows from their own offline sources. The base enrichment
    step then resolves each model's context window from the shared cascade, so
    CLI providers carry context like any HTTP one.
    """

    def models_provenance(self, ctx: HandshakeContext) -> tuple[str, str]:
        """Report ``static``: these rows come from an offline source, not a probe.

        This handshake makes zero network calls, so calling its output ``live``
        (the pre-fix behaviour, which stamped ``live`` whenever ANY model
        existed) claimed a probe that never happened -- and made a frozen
        snapshot of candidate ids indistinguishable from account evidence.
        """

        del ctx
        return "static", ""

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Report ``(OK, NOT_REQUIRED)`` without any network call.

        A CLI provider has no endpoint to reach and manages its own credentials.
        We report ``OK`` (not ``SKIPPED``) so the base flow proceeds to model
        discovery + context enrichment — there is nothing to fail on a local CLI,
        and binary presence is surfaced separately by the provider status check.
        """
        return ConnectivityResult(
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return no models: a CLI provider has no listing and no compiled-in candidates.

        Model rows come from a subclass's own data source (the #1211 refresh
        overlay, the maintained Claude Code catalog), never from a static
        registry list (model-capabilities brief 9.1).
        """
        del client, ctx
        return []

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> DiscoveredModelFacts:
        """Wrap an offline catalog row as :class:`DiscoveredModelFacts` (no network access).

        The row's ``capabilities`` become
        ``ModelCapabilities.input_modalities`` with ``source="catalog"`` — a
        cataloged candidate, never live evidence (this handshake makes zero
        network calls, matching :meth:`models_provenance`'s ``"static"``).
        Context/output limits are left unknown here; the base
        :meth:`~clio_agent.providers.handshake.base.ProviderHandshake.enrich_capabilities`
        step fills them from the community-catalog cascade.
        """
        model_id = str(raw.get("id", "")).strip()
        capabilities = raw.get("capabilities")
        caps = (
            tuple(
                str(value).strip()
                for value in capabilities
                if isinstance(value, str) and value.strip()
            )
            if isinstance(capabilities, list)
            else ()
        )
        observed_at = _now_iso()
        model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
        model_key = model_key_fact.value or model_id
        model = ModelCapabilities(
            model_key=model_key,
            input_modalities=Fact(
                value=modalities_from_capabilities(caps),
                source="catalog",
                observed_at=observed_at,
                detail="offline catalog row capabilities",
            )
            if caps
            else Fact(value=frozenset({"text"}), source="catalog", observed_at=observed_at),
        )
        deployment = DeploymentCapabilities(
            provider_id=ctx.provider_id,
            api_base=ctx.api_base,
            model_id=model_id,
            model_key=model_key_fact,
        )
        discovered = DiscoveredModel(id=model_id, raw={**dict(raw), "capabilities": list(caps)})
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)
